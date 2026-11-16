from outreach.services.linkedin import LinkedInScraper


def test_message_button_alone_cannot_prove_connection(monkeypatch):
    scraper = object.__new__(LinkedInScraper)
    class Page:
        def get_by_role(self, role, name):
            return name.pattern
        def locator(self, selector):
            return selector
    captured = []
    def visible(page, locators):
        captured.extend(locators)
        return any('Message' in locator for locator in locators)
    monkeypatch.setattr(scraper, '_has_visible_profile_action', visible)
    assert scraper._profile_has_connected_signal(Page()) is False
    assert captured and all('Remove connection' in locator for locator in captured)


def test_explicit_remove_connection_action_proves_connection(monkeypatch):
    scraper = object.__new__(LinkedInScraper)
    class Page:
        def get_by_role(self, role, name):
            return name.pattern
        def locator(self, selector):
            return selector
    monkeypatch.setattr(scraper, '_has_visible_profile_action',
                        lambda page, locators: 'Remove connection' in locators)
    assert scraper._profile_has_connected_signal(Page()) is True
