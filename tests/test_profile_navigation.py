"""Static regressions for the personal-settings navigation placement."""

from html.parser import HTMLParser
from pathlib import Path
import unittest


class NavigationParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.elements = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.elements.append((tag, attrs, list(self.stack)))
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input",
                       "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return


class ProfileNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (Path(__file__).resolve().parents[1] / "static/index.html").read_text()
        cls.parser = NavigationParser()
        cls.parser.feed(cls.html)

    def test_tools_live_in_profile_not_sidebar(self):
        for key in ("guideLink", "suggest", "adminTitle"):
            tools = [entry for entry in self.parser.elements
                     if entry[1].get("data-i18n") == key
                     and entry[0] in {"a", "button"}
                     and any(tag == "aside" or attrs.get("id") == "myProfile"
                             for tag, attrs in entry[2])]
            self.assertEqual(len(tools), 1, key)
            ancestors = tools[0][2]
            self.assertTrue(any(attrs.get("id") == "myProfile" for _, attrs in ancestors), key)
            self.assertFalse(any(tag == "aside" for tag, _ in ancestors), key)

    def test_admin_visibility_and_handlers_are_preserved(self):
        admins = [entry for entry in self.parser.elements if entry[1].get("id") == "adminBtn"]
        self.assertEqual(len(admins), 1)
        self.assertIn("hidden", admins[0][1]["class"].split())
        self.assertEqual(admins[0][1]["onclick"], "openAdmin()")
        self.assertIn('$("adminBtn").classList.toggle("hidden", !store.isSuperadmin)', self.html)
        guide = next(attrs for tag, attrs, _ in self.parser.elements
                     if tag == "a" and attrs.get("data-i18n") == "guideLink")
        self.assertEqual(guide["href"], "/guide")

    def test_other_sidebar_actions_remain(self):
        for handler in ("openFriends()", "openResources()", "openAgents()"):
            entries = [entry for entry in self.parser.elements if entry[1].get("onclick") == handler]
            self.assertTrue(any(any(tag == "aside" for tag, _ in entry[2]) for entry in entries))

    def test_personal_settings_labels_are_bilingual(self):
        self.assertIn('myProfileTitle: "个人设置"', self.html)
        self.assertIn('myProfileTitle: "Personal settings"', self.html)

    def test_room_actions_are_grouped_outside_governance(self):
        for action in ("autoReadBtn", "voiceBtn", "bringAgentBtn", "leaveRoomBtn"):
            elements = [entry for entry in self.parser.elements if entry[1].get("id") == action]
            self.assertEqual(len(elements), 1, action)
            ancestors = elements[0][2]
            self.assertTrue(any(attrs.get("id") == "roomQuickActions" for _, attrs in ancestors), action)
            self.assertFalse(any(attrs.get("id") == "ownerSettings" for _, attrs in ancestors), action)
        self.assertIn('$("manageBtn").classList.toggle("hidden", store.viewingArchive || !info.roomId)', self.html)
        files = [entry for entry in self.parser.elements if entry[1].get("id") == "filesBtn"]
        self.assertEqual(len(files), 1)
        self.assertTrue(any('chat-top' in attrs.get('class', '').split() for _, attrs in files[0][2]))
