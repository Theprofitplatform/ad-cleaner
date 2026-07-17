from gui import update_available


def test_newer_release_detected():
    assert update_available("v1.5.0", "1.4.0")
    assert update_available("2.0", "1.4.0")


def test_same_or_older_release_ignored():
    assert not update_available("v1.4.0", "1.4.0")
    assert not update_available("v1.3.9", "1.4.0")


def test_garbage_tags_ignored():
    assert not update_available("", "1.4.0")
    assert not update_available("latest", "1.4.0")
