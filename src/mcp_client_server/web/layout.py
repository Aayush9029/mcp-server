from html import escape

_CSS = """
:root{color-scheme:dark;--bg:#0b0c0f;--panel:#13151a;--line:#242833;--text:#e8eaf0;--dim:#9299a8;--accent:#8ec7ff}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);
font:16px/1.65 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",system-ui,sans-serif;
-webkit-font-smoothing:antialiased}
main{max-width:43rem;margin:0 auto;padding:4.5rem 1.5rem 2rem}
h1{font-size:2.1rem;line-height:1.15;letter-spacing:-.025em;margin:0 0 1rem;font-weight:650}
h2{font-size:1.05rem;letter-spacing:-.01em;margin:3rem 0 .75rem;font-weight:600}
h3{font-size:.8rem;letter-spacing:.06em;text-transform:uppercase;color:var(--dim);margin:1.75rem 0 .5rem;
font-weight:600}
p,li{color:#c9cdd8}
p{margin:.75rem 0}
.lead{font-size:1.15rem;color:var(--text);max-width:36rem}
ul,ol{padding-left:1.25rem;margin:.75rem 0}
li{margin:.3rem 0}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
code{font:.875em/1.5 ui-monospace,"SF Mono",Menlo,Consolas,monospace;color:var(--text)}
p code,li code{background:var(--panel);border:1px solid var(--line);border-radius:5px;padding:.1em .35em}
pre{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:.9rem 1rem;margin:.5rem 0 1rem;
overflow-x:auto}
pre code{font-size:.82rem}
.button{display:inline-block;margin-top:.75rem;padding:.6rem 1.1rem;border-radius:999px;background:var(--text);
color:var(--bg);font-weight:600;font-size:.95rem}
.button:hover{text-decoration:none;opacity:.9}
.dim{color:var(--dim);font-size:.9rem}
nav{max-width:43rem;margin:0 auto;padding:1.5rem 1.5rem 0;display:flex;gap:1.25rem;font-size:.9rem}
nav a{color:var(--dim)}
nav a.home{color:var(--text);font-weight:600;margin-right:auto}
footer{max-width:43rem;margin:0 auto;padding:2rem 1.5rem 4rem;color:var(--dim);font-size:.85rem;
border-top:1px solid var(--line)}
"""


def page(app_name: str, title: str, description: str, body: str) -> str:
    name = escape(app_name)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="{escape(description)}">
<title>{escape(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<nav>
<a class="home" href="/">{name}</a>
<a href="/support">Support</a>
<a href="/privacy">Privacy</a>
<a href="/terms">Terms</a>
</nav>
<main>
{body}
</main>
<footer>{name}</footer>
</body>
</html>
"""


def code(text: str) -> str:
    return f"<pre><code>{escape(text)}</code></pre>"
