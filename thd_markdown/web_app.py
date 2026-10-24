"""Standalone Markdown Web launcher; leaves existing editor sessions intact."""

import sys
from .app import Application


class WebApplication(Application):
    def __init__(self):
        super().__init__()
        self.set_application_id("io.github.the_hidden_door_llc.thd_markdown.web")

    def do_activate(self):
        self.browser_window().present()


if __name__ == "__main__":
    raise SystemExit(WebApplication().run(sys.argv))
