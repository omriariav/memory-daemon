"""Inbox 0.13.4: Option+Left/Right jump a word in every text field.

iTerm2 sends Option+Left as `alt+left`; Textual binds the word jumps to
`ctrl+left` only, so the inbox's WordInput and WordTextArea add the alt keys.
Synthetic text only."""
import importlib.util
import unittest

HAVE_TEXTUAL = importlib.util.find_spec("textual") is not None
if HAVE_TEXTUAL:
    from textual.app import App
    from textual.widgets import Input, TextArea
    from workspace_daemon.inbox.screens import RowMenu
    from workspace_daemon.inbox.widgets import WordInput, WordTextArea

try:    # run by discovery (tests/ on sys.path) or by dotted module name
    from test_inbox_draft import wait_for
    from test_inbox_rowmenu import go, make_app, press_text, settle
except ImportError:
    from tests.test_inbox_draft import wait_for
    from tests.test_inbox_rowmenu import go, make_app, press_text, settle

TEXT = "hello brave world"


if HAVE_TEXTUAL:
    class Host(App):
        def compose(self):
            yield WordInput(TEXT, id="inp")
            yield WordTextArea(TEXT, id="ta")


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class WordInputKeysTest(unittest.IsolatedAsyncioTestCase):
    async def test_alt_left_and_right_jump_a_word(self):
        async with Host().run_test(size=(80, 20)) as pilot:
            inp = pilot.app.query_one("#inp", WordInput)
            inp.focus()
            inp.cursor_position = len(TEXT)
            await pilot.press("alt+left")
            self.assertEqual(inp.cursor_position, TEXT.index("world"))
            await pilot.press("alt+left")
            self.assertEqual(inp.cursor_position, TEXT.index("brave"))
            await pilot.press("alt+right")
            # an Input's right jump lands on the start of the next word
            self.assertEqual(inp.cursor_position, TEXT.index("world"))

    async def test_ctrl_left_still_jumps(self):
        async with Host().run_test(size=(80, 20)) as pilot:
            inp = pilot.app.query_one("#inp", WordInput)
            inp.focus()
            inp.cursor_position = len(TEXT)
            await pilot.press("ctrl+left")
            self.assertEqual(inp.cursor_position, TEXT.index("world"))

    async def test_alt_shift_selects_a_word(self):
        async with Host().run_test(size=(80, 20)) as pilot:
            inp = pilot.app.query_one("#inp", WordInput)
            inp.focus()
            inp.cursor_position = len(TEXT)
            await pilot.press("alt+shift+left")
            self.assertEqual(inp.selected_text, "world")
            await pilot.press("alt+shift+right")
            self.assertEqual(inp.selected_text, "")

    async def test_alt_backspace_deletes_the_word_to_the_left(self):
        async with Host().run_test(size=(80, 20)) as pilot:
            inp = pilot.app.query_one("#inp", WordInput)
            inp.focus()
            inp.cursor_position = len(TEXT)
            await pilot.press("alt+backspace")
            self.assertEqual(inp.value, "hello brave ")

    async def test_alt_delete_deletes_the_word_to_the_right(self):
        async with Host().run_test(size=(80, 20)) as pilot:
            inp = pilot.app.query_one("#inp", WordInput)
            inp.focus()
            inp.cursor_position = 0
            await pilot.press("alt+delete")
            self.assertEqual(inp.value, "brave world")    # the word and its space


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class WordTextAreaKeysTest(unittest.IsolatedAsyncioTestCase):
    async def test_alt_left_and_right_jump_a_word(self):
        async with Host().run_test(size=(80, 20)) as pilot:
            ta = pilot.app.query_one("#ta", WordTextArea)
            ta.focus()
            ta.move_cursor((0, len(TEXT)))
            await pilot.press("alt+left")
            self.assertEqual(ta.cursor_location, (0, TEXT.index("world")))
            await pilot.press("alt+left")
            self.assertEqual(ta.cursor_location, (0, TEXT.index("brave")))
            await pilot.press("alt+right")
            self.assertEqual(ta.cursor_location, (0, TEXT.index(" world")))

    async def test_ctrl_left_still_jumps(self):
        async with Host().run_test(size=(80, 20)) as pilot:
            ta = pilot.app.query_one("#ta", WordTextArea)
            ta.focus()
            ta.move_cursor((0, len(TEXT)))
            await pilot.press("ctrl+left")
            self.assertEqual(ta.cursor_location, (0, TEXT.index("world")))

    async def test_alt_shift_selects_and_alt_backspace_deletes(self):
        async with Host().run_test(size=(80, 20)) as pilot:
            ta = pilot.app.query_one("#ta", WordTextArea)
            ta.focus()
            ta.move_cursor((0, len(TEXT)))
            await pilot.press("alt+shift+left")
            self.assertEqual(ta.selected_text, "world")
            await pilot.press("alt+right")      # drops the selection
            await pilot.press("alt+backspace")
            self.assertEqual(ta.text, "hello brave ")


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class InboxFieldsTest(unittest.IsolatedAsyncioTestCase):
    """The real inbox: its fields are the word-key widgets, the type
    selectors still find them, and no screen binding takes the keys."""

    async def test_filter_field_jumps_a_word(self):
        app = make_app()
        async with app.run_test(size=(150, 50)) as pilot:
            await settle(pilot, app)
            self.assertIsInstance(app.query_one("#filter", Input), WordInput)
            await pilot.press("slash")
            self.assertTrue(await wait_for(
                pilot, lambda: app.focused is app.query_one("#filter")))
            await press_text(pilot, TEXT)
            await pilot.press("alt+left")
            self.assertEqual(app.query_one("#filter", Input).cursor_position,
                             TEXT.index("world"))

    async def test_row_menu_filter_jumps_a_word(self):
        app = make_app()
        async with app.run_test(size=(150, 50)) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "cmd:ask")
            await pilot.press("enter")
            self.assertTrue(await wait_for(
                pilot, lambda: isinstance(app.screen, RowMenu)))
            await pilot.press("slash")
            box = app.screen.query_one("#rowmenu-filter", Input)
            self.assertTrue(await wait_for(pilot, lambda: app.focused is box))
            await press_text(pilot, TEXT)
            await pilot.press("alt+left")
            self.assertEqual(box.cursor_position, TEXT.index("world"))

    async def test_draft_editor_jumps_a_word(self):
        app = make_app()
        async with app.run_test(size=(150, 50)) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "cmd:ask")
            app._open_editor(TEXT)
            ta = app.query_one("#draft-edit", TextArea)
            self.assertIsInstance(ta, WordTextArea)
            self.assertTrue(await wait_for(pilot, lambda: app.focused is ta))
            await pilot.press("alt+left")
            self.assertEqual(ta.cursor_location, (0, TEXT.index("world")))


if __name__ == "__main__":
    unittest.main()
