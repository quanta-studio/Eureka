package droid.agent;

import android.accessibilityservice.AccessibilityServiceInfo;
import android.app.UiAutomation;
import android.graphics.Bitmap;
import android.os.Build;
import android.os.Bundle;
import android.os.HandlerThread;
import android.os.Looper;
import android.os.SystemClock;
import android.util.Log;
import android.view.InputDevice;
import android.view.InputEvent;
import android.view.KeyCharacterMap;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.lang.reflect.Constructor;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/** One UiAutomation connection, held for the life of the process, and everything done through it. */
final class Device {
    private static final long CONNECT_TIMEOUT_MS = 5_000;
    private static final long LAUNCH_TIMEOUT_MS = 10_000;
    private static final long FIELD_WAIT_MS = 2_000; // for a focused field to take typed text

    private final UiAutomation ui;
    private final Object lock = new Object();
    private long lastEvent; // uptime of the last accessibility event that means the UI changed
    private long lastAction; // uptime at which the last action finished
    private final Map<String, Long> lastEventFrom = new HashMap<>(); // package -> uptime of its last event
    private int events; // since the last takeEventCount(), for diagnosing settle()

    private Device(UiAutomation ui) {
        this.ui = ui;
        ui.setOnAccessibilityEventListener(event -> {
            int type = event.getEventType();
            if (type == AccessibilityEvent.TYPE_NOTIFICATION_STATE_CHANGED
                    || type == AccessibilityEvent.TYPE_ANNOUNCEMENT) {
                return; // neither changes what is on screen
            }
            synchronized (lock) {
                lastEvent = SystemClock.uptimeMillis();
                if (event.getPackageName() != null) {
                    lastEventFrom.put(event.getPackageName().toString(), lastEvent);
                }
                events++;
                lock.notifyAll();
            }
        });
    }

    int takeEventCount() {
        synchronized (lock) {
            int n = events;
            events = 0;
            return n;
        }
    }

    /** Connects the way `uiautomator` does, which the shell user is allowed to. */
    /**
     * Connects the way `uiautomator` does, which the shell user is allowed to. Only one client may hold the
     * connection, and while another does, connect() returns without error but never connects (Android 16), so the
     * result is checked rather than trusted. An agent told to quit a moment ago may still be letting go, hence the
     * retries, each with fresh objects since a failed connection cannot be reused.
     */
    static Device connect() throws Exception {
        HandlerThread events = new HandlerThread("a11y-events");
        events.start();
        Class<?> connectionClass = Class.forName("android.app.UiAutomationConnection");
        Constructor<?> newConnection = connectionClass.getDeclaredConstructor();
        newConnection.setAccessible(true);
        Constructor<UiAutomation> newAutomation = UiAutomation.class.getDeclaredConstructor(
                Looper.class, Class.forName("android.app.IUiAutomationConnection"));
        newAutomation.setAccessible(true);
        // Leave TalkBack and other accessibility services running, unlike plain connect().
        Method connect = UiAutomation.class.getDeclaredMethod("connect", int.class);
        connect.setAccessible(true);
        long deadline = SystemClock.uptimeMillis() + CONNECT_TIMEOUT_MS;
        Throwable refused = null;
        while (true) {
            Object connection = newConnection.newInstance();
            UiAutomation ui = newAutomation.newInstance(events.getLooper(), connection);
            try {
                connect.invoke(ui, UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES);
                if (connected(ui, 1000)) {
                    AccessibilityServiceInfo info = ui.getServiceInfo();
                    info.flags |= AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS
                            | AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS
                            | AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS;
                    ui.setServiceInfo(info);
                    return new Device(ui);
                }
            } catch (InvocationTargetException e) {
                refused = e.getCause();
            }
            try {
                connectionClass.getMethod("disconnect").invoke(connection); // give up our half of the registration
            } catch (ReflectiveOperationException | RuntimeException ignored) {
                // it never got that far
            }
            if (SystemClock.uptimeMillis() >= deadline) {
                throw new IllegalStateException("could not connect to UI automation: something else on the device is"
                        + " using it, such as uiautomator, an Appium session or an instrumentation test"
                        + (refused == null ? "" : " (" + refused + ")"));
            }
            Thread.sleep(200);
        }
    }

    private static boolean connected(UiAutomation ui, long timeoutMs) {
        long end = SystemClock.uptimeMillis() + timeoutMs;
        while (true) {
            try {
                ui.getServiceInfo(); // throws until the connection is up
                return true;
            } catch (IllegalStateException notYet) {
                if (SystemClock.uptimeMillis() >= end) {
                    return false;
                }
                SystemClock.sleep(20);
            }
        }
    }

    void disconnect() {
        try {
            UiAutomation.class.getMethod("disconnect").invoke(ui);
        } catch (ReflectiveOperationException | RuntimeException ignored) {
            // already gone
        }
    }

    // ---------- observation ----------

    /**
     * Waits until no accessibility event has arrived for quietMs, counting from the end of the last action too, so
     * a screen that has not started reacting yet is not mistaken for a settled one. False if timeoutMs ran out.
     */
    boolean settle(long quietMs, long timeoutMs) throws InterruptedException {
        long start = SystemClock.uptimeMillis();
        synchronized (lock) {
            while (true) {
                long now = SystemClock.uptimeMillis();
                long quietFor = now - Math.max(lastEvent, lastAction);
                if (quietFor >= quietMs) {
                    return true;
                }
                if (now - start >= timeoutMs) {
                    return false;
                }
                lock.wait(Math.max(1, Math.min(quietMs - quietFor, timeoutMs - (now - start))));
            }
        }
    }

    /** The UI in `uiautomator dump --windows` format. fresh drops the node cache, as a new uiautomator would. */
    String dump(boolean fresh) {
        if (fresh && Build.VERSION.SDK_INT >= 34) {
            ui.clearCache();
        }
        return Dump.windows(ui);
    }

    byte[] screenshot(int maxWidth, int quality, JSONObject reply) throws Exception {
        Bitmap shot = ui.takeScreenshot();
        if (shot == null) {
            throw new IllegalStateException("the device returned no screenshot");
        }
        if (shot.getConfig() == Bitmap.Config.HARDWARE) {
            shot = shot.copy(Bitmap.Config.ARGB_8888, false);
        }
        double scale = 1.0;
        if (shot.getWidth() > maxWidth) {
            scale = shot.getWidth() / (double) maxWidth;
            shot = Bitmap.createScaledBitmap(shot, maxWidth, (int) Math.round(shot.getHeight() / scale), true);
        }
        ByteArrayOutputStream jpeg = new ByteArrayOutputStream(64 * 1024);
        shot.compress(Bitmap.CompressFormat.JPEG, quality, jpeg);
        reply.put("scale", scale);
        return jpeg.toByteArray();
    }

    /** The foreground activity as package/.Class, like dumpsys prints it, or null if the system won't say. */
    String activity() {
        try {
            Object service = Build.VERSION.SDK_INT >= 29
                    ? Class.forName("android.app.ActivityTaskManager").getMethod("getService").invoke(null)
                    : Class.forName("android.app.ActivityManager").getMethod("getService").invoke(null);
            for (Method m : service.getClass().getMethods()) {
                Class<?>[] params = m.getParameterTypes();
                if (!m.getName().equals("getTasks") || params.length == 0 || params[0] != int.class) {
                    continue;
                }
                // getTasks(maxNum[, filterOnlyVisibleRecents, keepIntentExtra[, displayId]]), depending on version
                Object[] args = new Object[params.length];
                for (int i = 0; i < params.length; i++) {
                    args[i] = params[i] == boolean.class ? (Object) false : (Object) (i == 0 ? 1 : -1);
                }
                List<?> tasks = (List<?>) m.invoke(service, args);
                if (tasks == null || tasks.isEmpty()) {
                    return null;
                }
                Object top = tasks.get(0).getClass().getField("topActivity").get(tasks.get(0));
                return top == null ? null : ((android.content.ComponentName) top).flattenToShortString();
            }
        } catch (Exception e) {
            Log.w(Server.TAG, "no foreground activity", e);
        }
        return null;
    }

    // ---------- actions ----------
    //
    // Every event but the last of a gesture is queued without waiting. Waiting on each one lets a busy app stretch
    // the gesture out, and a swipe whose moves arrive late reads as a tap. The last event waits until the app has
    // handled it, so whatever comes next sees the effect.

    void tap(int x, int y) {
        long down = SystemClock.uptimeMillis();
        touch(MotionEvent.ACTION_DOWN, down, down, x, y, 1f, false);
        touch(MotionEvent.ACTION_UP, down, down, x, y, 0f, true);
        acted();
    }

    /** A straight line at constant speed over ms milliseconds, with a move every 8 ms as a 120 Hz panel reports. */
    void swipe(int x1, int y1, int x2, int y2, int ms) {
        long down = SystemClock.uptimeMillis();
        touch(MotionEvent.ACTION_DOWN, down, down, x1, y1, 1f, false);
        int moves = Math.max(1, ms / 8);
        for (int i = 1; i <= moves; i++) {
            long when = down + (long) ms * i / moves;
            SystemClock.sleep(Math.max(0, when - SystemClock.uptimeMillis()));
            float a = i / (float) moves;
            touch(MotionEvent.ACTION_MOVE, down, when, x1 + (x2 - x1) * a, y1 + (y2 - y1) * a, 1f, false);
        }
        touch(MotionEvent.ACTION_UP, down, down + ms, x2, y2, 0f, true);
        acted();
    }

    void key(int code) {
        long now = SystemClock.uptimeMillis();
        KeyEvent down = new KeyEvent(now, now, KeyEvent.ACTION_DOWN, code, 0, 0,
                KeyCharacterMap.VIRTUAL_KEYBOARD, 0, 0, InputDevice.SOURCE_KEYBOARD);
        inject(down, false);
        inject(KeyEvent.changeAction(down, KeyEvent.ACTION_UP), true);
        acted();
    }

    /**
     * Starts the package's launcher activity the way `monkey` does, then waits for the first accessibility event
     * from what came to the front: the activity is on top well before it has drawn anything, and the quiet period
     * should start after. That is not always the package's own window: resuming an existing task brings back
     * whatever it last showed, which can belong to another package (Samsung's settings search, a permission
     * dialog).
     */
    void launch(String pkg) throws Exception {
        long start = SystemClock.uptimeMillis();
        String before = activity();
        boolean already = before != null && before.startsWith(pkg + "/");
        Process monkey = new ProcessBuilder("monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1")
                .redirectErrorStream(true)
                .start();
        String output = new String(readAll(monkey.getInputStream()), StandardCharsets.UTF_8).trim();
        if (monkey.waitFor() != 0 || output.contains("No activities found")) {
            throw new IllegalStateException("could not launch " + pkg + ": " + output);
        }
        String after = activity();
        String front = after == null ? pkg : after.substring(0, after.indexOf('/'));
        synchronized (lock) {
            // An app that was already in front may have nothing new to draw.
            while (!already && lastEventFrom.getOrDefault(pkg, 0L) < start
                    && lastEventFrom.getOrDefault(front, 0L) < start) {
                long left = start + LAUNCH_TIMEOUT_MS - SystemClock.uptimeMillis();
                if (left <= 0) {
                    break; // observe whatever is there; the agent can look again
                }
                lock.wait(left);
            }
        }
        acted();
    }

    private static byte[] readAll(InputStream in) throws IOException {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] buf = new byte[4096];
        for (int n; (n = in.read(buf)) != -1; ) {
            out.write(buf, 0, n);
        }
        return out.toByteArray();
    }

    /**
     * Types into the focused field by inserting at its cursor through accessibility. Key events, the way `input
     * text` types, only cover ASCII and are silently lost while a field that has just appeared is still getting
     * ready; that can take half a second after the screen has gone quiet. An insertion the field refuses changes
     * nothing, so it is retried until the field takes it.
     *
     * <p>Key events remain the way into password fields, whose text accessibility won't reveal, and into anything
     * that takes keys without exposing a text field.
     */
    void text(String s) {
        long deadline = SystemClock.uptimeMillis() + FIELD_WAIT_MS;
        AccessibilityNodeInfo field;
        while (true) {
            field = ui.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
            boolean editable = field != null && field.isEditable();
            if (editable && field.isPassword()) {
                break;
            }
            if (editable && insert(field, s)) {
                acted();
                return;
            }
            if (SystemClock.uptimeMillis() >= deadline) {
                break;
            }
            SystemClock.sleep(50);
        }
        KeyEvent[] events = KeyCharacterMap.load(KeyCharacterMap.VIRTUAL_KEYBOARD).getEvents(s.toCharArray());
        if (events == null) {
            throw new IllegalStateException(field == null || !field.isEditable()
                    ? "no focused text field to type into; tap one first"
                    : field.isPassword()
                    ? "a password field only takes characters on the keyboard"
                    : "the focused field refused the text");
        }
        for (int k = 0; k < events.length; k++) {
            inject(events[k], k == events.length - 1);
        }
        acted();
    }

    /** Inserts s at the field's cursor, replacing any selection. False if the field refused. */
    private boolean insert(AccessibilityNodeInfo field, String s) {
        field.refresh();
        CharSequence current = field.isShowingHintText() || field.getText() == null ? "" : field.getText();
        int start = Math.min(field.getTextSelectionStart(), field.getTextSelectionEnd());
        int end = Math.max(field.getTextSelectionStart(), field.getTextSelectionEnd());
        if (start < 0 || end > current.length()) {
            start = end = current.length();
        }
        Bundle text = new Bundle();
        text.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,
                current.subSequence(0, start) + s + current.subSequence(end, current.length()));
        if (!field.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, text)) {
            return false;
        }
        Bundle caret = new Bundle();
        caret.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, start + s.length());
        caret.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, start + s.length());
        field.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, caret);
        return true;
    }

    private void touch(int action, long down, long when, float x, float y, float pressure, boolean wait) {
        MotionEvent.PointerProperties[] props = {new MotionEvent.PointerProperties()};
        props[0].id = 0;
        props[0].toolType = MotionEvent.TOOL_TYPE_FINGER;
        MotionEvent.PointerCoords[] coords = {new MotionEvent.PointerCoords()};
        coords[0].x = x;
        coords[0].y = y;
        coords[0].pressure = pressure;
        coords[0].size = 1f;
        MotionEvent e = MotionEvent.obtain(down, when, action, 1, props, coords, 0, 0, 1f, 1f, 0, 0,
                InputDevice.SOURCE_TOUCHSCREEN, 0);
        try {
            inject(e, wait);
        } finally {
            e.recycle();
        }
    }

    /** With wait, returns once the target window has handled the event; without, once it is queued. */
    private void inject(InputEvent e, boolean wait) {
        if (!ui.injectInputEvent(e, wait)) {
            throw new IllegalStateException("the system refused to inject " + e);
        }
    }

    private void acted() {
        synchronized (lock) {
            lastAction = SystemClock.uptimeMillis();
        }
    }
}
