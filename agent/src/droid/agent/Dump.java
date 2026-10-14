package droid.agent;

import android.app.UiAutomation;
import android.graphics.Point;
import android.graphics.Rect;
import android.os.Build;
import android.util.SparseArray;
import android.util.Xml;
import android.view.Display;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;

import org.xmlpull.v1.XmlSerializer;

import java.io.IOException;
import java.io.StringWriter;
import java.lang.reflect.Method;
import java.util.List;

/**
 * Writes the same XML as `uiautomator dump --windows` (AccessibilityNodeInfoDumper in AOSP), so droid_mcp parses
 * either one. The NAF attribute is left out: droid_mcp does not read it and it costs a second walk of each subtree.
 */
final class Dump {
    private Dump() {}

    /** Every window on every display, or "" when there are none (the screen is off). */
    static String windows(UiAutomation ui) {
        SparseArray<List<AccessibilityWindowInfo>> all;
        if (Build.VERSION.SDK_INT >= 30) {
            all = ui.getWindowsOnAllDisplays();
        } else {
            all = new SparseArray<>();
            all.put(0, ui.getWindows());
        }
        StringWriter xml = new StringWriter();
        boolean any = false;
        try {
            XmlSerializer out = Xml.newSerializer();
            out.setOutput(xml);
            out.startDocument("UTF-8", true);
            out.startTag("", "displays");
            for (int d = 0; d < all.size(); d++) {
                List<AccessibilityWindowInfo> windows = all.valueAt(d);
                Display display = realDisplay(all.keyAt(d));
                if (windows == null || windows.isEmpty() || display == null) {
                    continue;
                }
                any = true;
                Point size = new Point();
                display.getRealSize(size);
                out.startTag("", "display");
                out.attribute("", "id", Integer.toString(all.keyAt(d)));
                for (int i = 0; i < windows.size(); i++) {
                    window(out, windows.get(i), i, size.x, size.y, display.getRotation());
                }
                out.endTag("", "display");
            }
            out.endTag("", "displays");
            out.endDocument();
        } catch (IOException impossible) {
            throw new IllegalStateException(impossible);
        }
        return any ? xml.toString() : "";
    }

    private static void window(XmlSerializer out, AccessibilityWindowInfo w, int index, int width, int height,
            int rotation) throws IOException {
        out.startTag("", "window");
        out.attribute("", "index", Integer.toString(index));
        out.attribute("", "title", text(w.getTitle()));
        Rect bounds = new Rect();
        w.getBoundsInScreen(bounds);
        out.attribute("", "bounds", bounds.toShortString());
        out.attribute("", "active", Boolean.toString(w.isActive()));
        out.attribute("", "focused", Boolean.toString(w.isFocused()));
        out.attribute("", "accessibility-focused", Boolean.toString(w.isAccessibilityFocused()));
        out.attribute("", "id", Integer.toString(w.getId()));
        out.attribute("", "layer", Integer.toString(w.getLayer()));
        out.attribute("", "type", type(w.getType()));
        for (int i = 0; i < w.getChildCount(); i++) {
            AccessibilityWindowInfo child = w.getChild(i);
            if (child != null) {
                window(out, child, i, width, height, rotation);
            }
        }
        AccessibilityNodeInfo root = w.getRoot();
        if (root != null) {
            out.startTag("", "hierarchy");
            out.attribute("", "rotation", Integer.toString(rotation));
            node(out, root, 0, width, height);
            out.endTag("", "hierarchy");
        }
        out.endTag("", "window");
    }

    private static void node(XmlSerializer out, AccessibilityNodeInfo n, int index, int width, int height)
            throws IOException {
        out.startTag("", "node");
        out.attribute("", "index", Integer.toString(index));
        out.attribute("", "text", text(n.getText()));
        out.attribute("", "resource-id", text(n.getViewIdResourceName()));
        out.attribute("", "class", text(n.getClassName()));
        out.attribute("", "package", text(n.getPackageName()));
        out.attribute("", "content-desc", text(n.getContentDescription()));
        out.attribute("", "checkable", Boolean.toString(n.isCheckable()));
        out.attribute("", "checked", Boolean.toString(n.isChecked()));
        out.attribute("", "clickable", Boolean.toString(n.isClickable()));
        out.attribute("", "enabled", Boolean.toString(n.isEnabled()));
        out.attribute("", "focusable", Boolean.toString(n.isFocusable()));
        out.attribute("", "focused", Boolean.toString(n.isFocused()));
        out.attribute("", "scrollable", Boolean.toString(n.isScrollable()));
        out.attribute("", "long-clickable", Boolean.toString(n.isLongClickable()));
        out.attribute("", "password", Boolean.toString(n.isPassword()));
        out.attribute("", "selected", Boolean.toString(n.isSelected()));
        Rect bounds = new Rect();
        n.getBoundsInScreen(bounds);
        if (!bounds.intersect(0, 0, width, height)) {
            bounds.setEmpty();
        }
        out.attribute("", "bounds", bounds.toShortString());
        out.attribute("", "drawing-order", Integer.toString(n.getDrawingOrder()));
        out.attribute("", "hint", text(n.getHintText()));
        for (int i = 0; i < n.getChildCount(); i++) {
            AccessibilityNodeInfo child = n.getChild(i);
            if (child != null && child.isVisibleToUser()) {
                node(out, child, i, width, height);
            }
        }
        out.endTag("", "node");
    }

    /** XmlSerializer rejects control characters; uiautomator writes them as dots, and so do we. */
    private static String text(CharSequence cs) {
        if (cs == null) {
            return "";
        }
        StringBuilder s = new StringBuilder(cs.length());
        for (int i = 0; i < cs.length(); i++) {
            char c = cs.charAt(i);
            boolean invalid = (c >= 0x1 && c <= 0x8) || (c >= 0xB && c <= 0xC) || (c >= 0xE && c <= 0x1F)
                    || (c >= 0x7F && c <= 0x84) || (c >= 0x86 && c <= 0x9F) || (c >= 0xFDD0 && c <= 0xFDDF);
            s.append(invalid ? '.' : c);
        }
        return s.toString();
    }

    private static String type(int type) {
        switch (type) {
            case AccessibilityWindowInfo.TYPE_APPLICATION:
                return "TYPE_APPLICATION";
            case AccessibilityWindowInfo.TYPE_INPUT_METHOD:
                return "TYPE_INPUT_METHOD";
            case AccessibilityWindowInfo.TYPE_SYSTEM:
                return "TYPE_SYSTEM";
            case AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY:
                return "TYPE_ACCESSIBILITY_OVERLAY";
            case AccessibilityWindowInfo.TYPE_SPLIT_SCREEN_DIVIDER:
                return "TYPE_SPLIT_SCREEN_DIVIDER";
            case 6:
                return "TYPE_MAGNIFICATION_OVERLAY";
            default:
                return "<UNKNOWN:" + type + ">";
        }
    }

    private static Method getRealDisplay;
    private static Object displayManager;

    private static Display realDisplay(int id) {
        try {
            if (getRealDisplay == null) {
                Class<?> global = Class.forName("android.hardware.display.DisplayManagerGlobal");
                displayManager = global.getMethod("getInstance").invoke(null);
                getRealDisplay = global.getMethod("getRealDisplay", int.class);
            }
            return (Display) getRealDisplay.invoke(displayManager, id);
        } catch (ReflectiveOperationException e) {
            throw new IllegalStateException("no DisplayManagerGlobal on this Android version", e);
        }
    }
}
