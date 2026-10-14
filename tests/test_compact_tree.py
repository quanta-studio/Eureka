from tests.fakes import hierarchy, node


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
