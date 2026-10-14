from tests.fakes import hierarchy, node, window, windows


def test_lists_labelled_and_actionable_nodes_and_skips_bare_containers(device):
    xml = hierarchy(
        node(cls="android.widget.FrameLayout", bounds="[0,0][1080,2400]"),
        node(text="Settings", bounds="[0,100][1080,200]"),
        node(
            cls="android.widget.ImageButton",
            content_desc="Search",
            resource_id="com.android.settings:id/search_button",
            clickable=True,
            bounds="[900,100][1080,200]",
        ),
        node(cls="android.widget.EditText", bounds="[0,300][1080,400]"),
    )

    assert device.backend.compact_tree(xml).splitlines() == [
        '[0] TextView "Settings"',
        '[1] ImageButton "Search" id=search_button tap',
        '[2] EditText ""',
    ]


def test_nested_nodes_are_flattened_in_document_order(device):
    xml = hierarchy(
        node(
            node(text="First"),
            node(node(text="Second"), cls="android.widget.LinearLayout"),
            cls="android.widget.FrameLayout",
        ),
        node(text="Third"),
    )

    assert device.backend.compact_tree(xml).splitlines() == [
        '[0] TextView "First"',
        '[1] TextView "Second"',
        '[2] TextView "Third"',
    ]


def test_index_maps_to_the_element_centre_in_device_pixels(device):
    xml = hierarchy(node(text="A", bounds="[0,100][200,300]"), node(text="B", bounds="[540,1200][1080,2400]"))

    device.backend.compact_tree(xml)

    assert device.backend.elements == {0: (100, 200), 1: (810, 1800)}


def test_nodes_without_a_usable_area_are_skipped_and_do_not_consume_an_index(device):
    xml = hierarchy(
        node(text="collapsed", bounds="[10,10][10,50]"),
        node(text="inverted", bounds="[50,50][40,40]"),
        node(text="unbounded", bounds=""),
        node(text="visible", bounds="[0,0][10,10]"),
    )

    assert device.backend.compact_tree(xml) == '[0] TextView "visible"'
    assert device.backend.elements == {0: (5, 5)}


def test_label_prefers_text_falls_back_to_content_desc_and_stays_on_one_short_line(device):
    xml = hierarchy(
        node(text="Wi-Fi", content_desc="ignored"),
        node(content_desc="Back"),
        node(text="line one\nline two"),
        node(text="x" * 200),
    )

    assert device.backend.compact_tree(xml).splitlines() == [
        '[0] TextView "Wi-Fi"',
        '[1] TextView "Back"',
        '[2] TextView "line one line two"',
        f'[3] TextView "{"x" * 60}"',
    ]


def test_state_flags_are_reported_in_a_fixed_order(device):
    xml = hierarchy(
        node(text="all", clickable=True, scrollable=True, checked=True, focused=True),
        node(text="off", clickable=False, scrollable=False, checked=False, focused=False),
        node(cls="android.view.View", long_clickable=True),
    )

    assert device.backend.compact_tree(xml).splitlines() == [
        '[0] TextView "all" tap,scroll,checked,focused',
        '[1] TextView "off"',
        '[2] View ""',
    ]


def test_empty_tree_points_the_agent_at_screenshots(device):
    out = device.backend.compact_tree(hierarchy(node(cls="android.view.SurfaceView", bounds="[0,0][1080,2400]")))

    assert "tap_xy" in out
    assert device.backend.elements == {}


def test_a_new_observation_replaces_the_previous_indices(device):
    device.backend.compact_tree(hierarchy(node(text="A"), node(text="B"), node(text="C")))

    device.backend.compact_tree(hierarchy(node(text="Z", bounds="[0,0][20,20]")))

    assert device.backend.elements == {0: (10, 10)}


# --- what is drawn on top -------------------------------------------------------------------------------------------
# Geometry below is taken from a Samsung Settings screen, where a floating search pill sits over the last visible row.

ROW, LABEL, PILL = "[18,1433][702,1538]", "[135,1463][260,1507]", "[131,1455][588,1553]"
SETTINGS_ROW_UNDER_PILL = hierarchy(
    node(node(text="Display", bounds=LABEL), cls="android.widget.LinearLayout", clickable=True, bounds=ROW),
    node(cls="android.widget.LinearLayout", resource_id="x:id/search_plate", clickable=True, bounds=PILL),
)


def inside(point, bounds):
    x1, y1, x2, y2 = map(int, bounds.replace("][", ",").strip("[]").split(","))
    return x1 <= point[0] < x2 and y1 <= point[1] < y2


def test_tap_point_moves_off_a_control_drawn_over_the_element(device):
    device.backend.compact_tree(SETTINGS_ROW_UNDER_PILL)

    row = device.backend.elements[0]

    assert inside(row, ROW)
    assert not inside(row, PILL)


def test_a_covered_label_is_tapped_through_the_exposed_part_of_its_row(device):
    tree = device.backend.compact_tree(SETTINGS_ROW_UNDER_PILL)

    label = device.backend.elements[1]

    assert tree.splitlines()[1] == '[1] TextView "Display"'
    assert inside(label, ROW)
    assert not inside(label, PILL)


def test_an_element_with_nothing_exposed_is_flagged_and_has_no_tap_point(device):
    xml = hierarchy(
        node(cls="android.widget.Button", text="Buy", clickable=True, bounds="[100,100][300,200]"),
        node(cls="android.view.View", content_desc="Scrim", clickable=True, bounds="[0,0][1080,2400]"),
    )

    tree = device.backend.compact_tree(xml)

    assert tree.splitlines() == ['[0] Button "Buy" tap,covered', '[1] View "Scrim" tap']
    assert 0 not in device.backend.elements
    assert device.backend.elements[1] == (540, 1200)


def test_a_sliver_left_exposed_is_not_treated_as_a_tap_target(device):
    xml = hierarchy(
        node(cls="android.widget.Button", text="Buy", clickable=True, bounds="[100,100][300,200]"),
        node(cls="android.view.View", content_desc="Sheet", clickable=True, bounds="[0,104][1080,2400]"),
    )

    assert device.backend.compact_tree(xml).splitlines()[0] == '[0] Button "Buy" tap,covered'


def test_a_container_is_not_covered_by_its_own_children(device):
    xml = hierarchy(
        node(
            node(text="Wi-Fi", clickable=True, bounds="[0,0][1080,1200]"),
            node(text="Bluetooth", clickable=True, bounds="[0,1200][1080,2400]"),
            cls="androidx.recyclerview.widget.RecyclerView",
            scrollable=True,
            bounds="[0,0][1080,2400]",
        )
    )

    tree = device.backend.compact_tree(xml)

    assert tree.splitlines()[0] == '[0] RecyclerView "" scroll'
    assert device.backend.elements[0] == (540, 1200)


def test_tapping_a_clickable_parent_avoids_its_clickable_child(device):
    xml = hierarchy(
        node(
            node(cls="android.widget.ImageView", content_desc="Delete", clickable=True, bounds="[400,0][700,200]"),
            cls="android.widget.LinearLayout",
            content_desc="Recent search",
            clickable=True,
            bounds="[0,0][1080,200]",
        )
    )

    device.backend.compact_tree(xml)

    assert inside(device.backend.elements[0], "[0,0][1080,200]")
    assert not inside(device.backend.elements[0], "[400,0][700,200]")
    assert device.backend.elements[1] == (550, 100)


def test_earlier_and_non_clickable_neighbours_do_not_cover_an_element(device):
    xml = hierarchy(
        node(cls="android.view.View", content_desc="Backdrop", clickable=True, bounds="[0,0][1080,2400]"),
        node(cls="android.widget.Button", text="OK", clickable=True, bounds="[400,1000][680,1100]"),
        node(cls="android.widget.ImageView", content_desc="Shine", bounds="[0,0][1080,2400]"),
    )

    device.backend.compact_tree(xml)

    assert device.backend.elements[1] == (540, 1050)


# --- other windows --------------------------------------------------------------------------------------------------

STATUS_BAR = window(node(text="10:04", bounds="[20,10][120,60]"), bounds="[0,0][1080,142]", layer=2, kind="TYPE_SYSTEM")
KEYBOARD = window(
    node(text="q", clickable=True, bounds="[0,1600][108,1750]"),
    bounds="[0,1541][1080,2400]",
    layer=1,
    kind="TYPE_INPUT_METHOD",
)


def app(*nodes):
    return window(*nodes, bounds="[0,0][1080,2400]", active=True)


def test_only_the_active_window_is_listed(device):
    xml = windows(STATUS_BAR, KEYBOARD, app(node(text="Results", bounds="[0,200][1080,300]")))

    assert device.backend.compact_tree(xml) == '[0] TextView "Results"'


def test_an_element_behind_the_keyboard_is_flagged_as_covered(device):
    xml = windows(
        KEYBOARD, app(node(cls="android.widget.Button", text="Send", clickable=True, bounds="[800,1800][1000,1900]"))
    )

    assert device.backend.compact_tree(xml) == '[0] Button "Send" tap,covered'
    assert device.backend.covered == {0: "the keyboard"}


def test_an_element_partly_behind_the_keyboard_is_tapped_above_it(device):
    xml = windows(KEYBOARD, app(node(text="Row", clickable=True, bounds="[0,1341][1080,1741]")))

    device.backend.compact_tree(xml)

    assert device.backend.elements[0] == (540, 1441)


def test_windows_below_the_active_one_cover_nothing(device):
    dialog = window(
        node(cls="android.widget.Button", text="Close app", clickable=True, bounds="[300,1200][780,1300]"),
        bounds="[100,900][980,1500]",
        layer=3,
        kind="TYPE_SYSTEM",
        active=True,
    )
    launcher = window(node(text="Phone", clickable=True, bounds="[0,0][1080,2400]"), bounds="[0,0][1080,2400]")

    assert device.backend.compact_tree(windows(dialog, launcher)) == '[0] Button "Close app" tap'
    assert device.backend.elements[0] == (540, 1250)


def test_keyboard_presence_is_tracked(device):
    device.backend.compact_tree(windows(KEYBOARD, app(node(text="Results"))))
    assert device.backend.keyboard is True

    device.backend.compact_tree(windows(STATUS_BAR, app(node(text="Results"))))
    assert device.backend.keyboard is False


def test_viewport_is_the_part_of_the_window_no_other_window_sits_over(device):
    device.backend.compact_tree(windows(STATUS_BAR, KEYBOARD, app(node(text="Results"))))
    assert device.backend.viewport == (0, 142, 1080, 1541)

    device.backend.compact_tree(hierarchy(node(text="Results")))
    assert device.backend.viewport is None


def test_a_dump_with_no_active_window_is_reported_as_such(device):
    tree = device.backend.compact_tree(windows(STATUS_BAR))

    assert "no active window" in tree
    assert device.backend.elements == {}
