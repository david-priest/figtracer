"""pdftocairo spells its page background ``rgb(100%, 100%, 100%)`` with spaces, and draws it
larger than the page. Until 2026-09-07 is_white missed the spaced form, so the assembler kept
the rect and every pdftocairo panel painted over the panel above it."""
from lxml import etree

from figtools import style, whites


def test_is_white_accepts_spaced_rgb():
    assert style.is_white("rgb(100%, 100%, 100%)")
    assert style.is_white("rgb(255, 255, 255)")
    assert style.is_white("#FFFFFF")
    assert not style.is_white("rgb(100%, 100%, 99%)")


def test_pdftocairo_page_rect_is_stripped():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="792pt" height="302pt" viewBox="0 0 792 302">'
           '<rect x="-79.2" y="-30.2" width="950.4" height="362.4" fill="rgb(100%, 100%, 100%)" fill-opacity="1"/>'
           '<rect x="10" y="10" width="50" height="50" fill="rgb(100%, 100%, 100%)"/>'
           '</svg>')
    root = etree.fromstring(svg.encode())
    log = whites.clean(root, level="backgrounds")
    assert log["backgrounds_removed"] == 1
    assert len(root) == 1 and root[0].get("x") == "10"
