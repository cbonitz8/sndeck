from sndeck.theme import MACCHIATO, LATTE, THEMES, next_theme


def test_two_themes_registered_with_names():
    names = {t.name for t in THEMES}
    assert names == {MACCHIATO, LATTE}


def test_macchiato_is_dark_latte_is_light():
    by = {t.name: t for t in THEMES}
    assert by[MACCHIATO].dark is True
    assert by[LATTE].dark is False


def test_key_colors():
    by = {t.name: t for t in THEMES}
    assert by[MACCHIATO].background == "#24273a"
    assert by[MACCHIATO].success == "#a6da95"
    assert by[LATTE].background == "#eff1f5"
    assert by[LATTE].primary == "#1e66f5"


def test_next_theme_toggles():
    assert next_theme(MACCHIATO) == LATTE
    assert next_theme(LATTE) == MACCHIATO
