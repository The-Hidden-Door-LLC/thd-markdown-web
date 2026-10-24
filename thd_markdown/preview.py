"""Offline Markdown rendering. Documents never supply active HTML."""

from markdown_it import MarkdownIt

PARSER = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table").enable("strikethrough")


def image_placeholder(tokens, index, options, env):
    from html import escape
    return '<span class="image-note">[Image: ' + escape(tokens[index].content) + ']</span>'


PARSER.add_render_rule("image", lambda self, tokens, index, options, env: image_placeholder(tokens, index, options, env))

STYLE = """
html { color-scheme: dark; background: #0a0d1a; color: #dfe6ff; }
body { max-width: 760px; padding: 32px 40px 90px; margin: auto;
  font: 17px/1.75 'Atkinson Hyperlegible', sans-serif; overflow-wrap: anywhere; }
h1,h2,h3,h4 { font-family: Sora, sans-serif; line-height: 1.3; letter-spacing: -.025em; }
h1 { font-size: 34px; margin: 0 0 24px; } h2 { margin-top: 1.8em; font-size: 24px; }
h3 { font-size: 19px; } a { color: #e7c66a; text-decoration: none; }
blockquote { margin-left: 0; border-left: 3px solid #e7c66a; padding: 4px 20px; color: #aebbdc; }
pre { padding: 18px; background: #11182b; border: 1px solid #23304d; border-radius: 10px; overflow-x:auto; }
code { font: .88em/1.7 monospace; background: #11182b; padding: 2px 5px; border-radius: 4px; }
pre code { padding: 0; } table { border-collapse: collapse; width:100%; }
th,td { border-bottom:1px solid #29334e; padding:10px; text-align:left; }
th { color:#e7c66a; } hr { border:0; border-top:1px solid #29334e; margin:30px 0; }
.image-note { color:#8d9abb; font-style:italic; } li { padding-left:4px; }
@media (max-width: 480px) { body { padding:24px 20px 60px; } h1 { font-size:28px; } }
"""


def render(text):
    return ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
            'style-src \'unsafe-inline\'; img-src \'none\'; font-src \'none\'; '
            'script-src \'none\'; connect-src \'none\'; frame-src \'none\'; base-uri \'none\'; form-action \'none\'">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<style>' + STYLE + '</style></head><body>' + PARSER.render(text) + '</body></html>')
