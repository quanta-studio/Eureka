package droid.agent;

import android.net.LocalServerSocket;
import android.net.LocalSocket;
import android.net.LocalSocketAddress;
import android.os.Build;
import android.os.Looper;
import android.util.Log;

import org.json.JSONObject;

import java.io.BufferedOutputStream;
import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;

/**
 * droid-agent: a process kept running on the device so a step costs no JVM start-up and no fixed idle wait.
 *
 * <p>Started by droid_mcp as {@code CLASSPATH=droid-agent.jar app_process / droid.agent.Server <token>}, which runs
 * it as the shell user: no root, no installed app. It listens on the abstract socket {@code droid-agent}, which the
 * host reaches through {@code adb forward}.
 *
 * <p>Protocol: each request is one line of JSON with a {@code cmd}. Each reply is one line of JSON with {@code ok},
 * followed by {@code len} raw bytes when the reply has that field (a UI dump or a JPEG).
 */
public final class Server {
    static final String TAG = "droid-agent";
    static final String SOCKET = "droid-agent";

    public static void main(String[] args) {
        try {
            run(args);
        } catch (Throwable e) {
            // app_process sends an uncaught exception to logcat only; the host reads this from stderr instead.
            System.err.println(e.getMessage() != null ? e.getMessage() : e.toString());
            System.err.flush();
            System.exit(1);
        }
    }

    private static void run(String[] args) throws Exception {
        String token = args.length > 0 ? args[0] : "";
        // The accessibility client posts to the main looper, which app_process does not create on its own.
        Looper.prepareMainLooper();
        // Bind before connecting UiAutomation: taking the socket over is how an older agent is told to let go.
        LocalServerSocket server = bind();
        Device device = Device.connect();
        // adb closes our stdin once the host side of `adb shell` is gone. Nothing else would ever stop us.
        Thread host = new Thread(() -> {
            try {
                while (System.in.read() != -1) {
                    // the host never writes; this only waits for end of input
                }
            } catch (IOException ignored) {
                // a broken pipe means the same thing
            }
            System.exit(0);
        }, "host-watch");
        host.setDaemon(true);
        host.start();
        Runtime.getRuntime().addShutdownHook(new Thread(device::disconnect));
        Thread accepting = new Thread(() -> {
            while (true) {
                try {
                    LocalSocket client = server.accept();
                    Thread serving = new Thread(() -> serve(device, client, token), "client");
                    serving.setDaemon(true);
                    serving.start();
                } catch (IOException e) {
                    Log.e(TAG, "accept failed", e);
                    System.exit(1);
                }
            }
        }, "accept");
        accepting.start();
        System.out.println("droid-agent ready");
        System.out.flush();
        Looper.loop();
    }

    private static LocalServerSocket bind() throws IOException, InterruptedException {
        for (int attempt = 0; ; attempt++) {
            try {
                return new LocalServerSocket(SOCKET);
            } catch (IOException taken) {
                if (attempt == 50) {
                    throw taken;
                }
                if (attempt == 0) {
                    askToQuit();
                }
                Thread.sleep(100);
            }
        }
    }

    /** Another agent holds the socket (a previous session, or one restored from a snapshot): ask it to exit. */
    private static void askToQuit() {
        try (LocalSocket other = new LocalSocket()) {
            other.connect(new LocalSocketAddress(SOCKET));
            other.getOutputStream().write("{\"cmd\":\"quit\"}\n".getBytes(StandardCharsets.UTF_8));
            other.getInputStream().read();
        } catch (IOException ignored) {
            // it is already gone
        }
    }

    private static void serve(Device device, LocalSocket client, String token) {
        try (LocalSocket c = client) {
            BufferedReader in = new BufferedReader(new InputStreamReader(c.getInputStream(), StandardCharsets.UTF_8));
            OutputStream out = new BufferedOutputStream(c.getOutputStream(), 1 << 16);
            for (String line; (line = in.readLine()) != null; ) {
                JSONObject reply = new JSONObject();
                byte[] payload = null;
                String cmd = "";
                try {
                    JSONObject request = new JSONObject(line);
                    cmd = request.getString("cmd");
                    payload = handle(device, cmd, request, reply, token);
                    reply.put("ok", true);
                } catch (Throwable e) {
                    Log.w(TAG, "request failed: " + line, e);
                    reply = new JSONObject();
                    reply.put("ok", false);
                    reply.put("error", e.getMessage() != null ? e.getMessage() : e.toString());
                    payload = null;
                }
                if (payload != null) {
                    reply.put("len", payload.length);
                }
                out.write((reply + "\n").getBytes(StandardCharsets.UTF_8));
                if (payload != null) {
                    out.write(payload);
                }
                out.flush();
                if (cmd.equals("quit")) {
                    System.exit(0);
                }
            }
        } catch (Exception e) {
            Log.w(TAG, "client dropped", e);
        }
    }

    private static byte[] handle(Device device, String cmd, JSONObject r, JSONObject reply, String token)
            throws Exception {
        switch (cmd) {
            case "hello":
                reply.put("token", token);
                reply.put("sdk", Build.VERSION.SDK_INT);
                return null;
            case "dump": {
                long started = System.nanoTime();
                reply.put("settled", device.settle(r.optLong("quiet_ms", 500), r.optLong("timeout_ms", 5000)));
                long settled = System.nanoTime();
                String xml = device.dump(r.optBoolean("fresh", true));
                reply.put("activity", device.activity());
                reply.put("settle_ms", (settled - started) / 1_000_000);
                reply.put("dump_ms", (System.nanoTime() - settled) / 1_000_000);
                reply.put("events", device.takeEventCount());
                return xml.getBytes(StandardCharsets.UTF_8);
            }
            case "screenshot":
                return device.screenshot(r.getInt("max_w"), r.optInt("quality", 80), reply);
            case "activity":
                reply.put("activity", device.activity());
                return null;
            case "tap":
                device.tap(r.getInt("x"), r.getInt("y"));
                return null;
            case "swipe":
                device.swipe(r.getInt("x1"), r.getInt("y1"), r.getInt("x2"), r.getInt("y2"), r.optInt("ms", 300));
                return null;
            case "key":
                device.key(r.getInt("code"));
                return null;
            case "text":
                device.text(r.getString("text"));
                return null;
            case "launch":
                device.launch(r.getString("package"));
                return null;
            case "quit":
                return null;
            default:
                throw new IllegalArgumentException("unknown command " + cmd);
        }
    }
}
