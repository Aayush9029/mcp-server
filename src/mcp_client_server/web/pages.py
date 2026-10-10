from html import escape

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from mcp_client_server.config import Settings
from mcp_client_server.limits import CLOSED_TASK_TTL_SECONDS
from mcp_client_server.web.layout import code, page

_UPDATED = "October 10, 2026"
_CACHE = {"Cache-Control": "public, max-age=300"}
_KEY = "mck_YOUR_KEY"


def _home(s: Settings) -> str:
    name, url = escape(s.app_name), s.mcp_url
    store = f'<p><a class="button" href="{escape(s.app_store_url)}">Get the app</a></p>' if s.app_store_url else ""
    cursor = (
        '{\n  "mcpServers": {\n    "tasks": {\n'
        f'      "url": "{url}",\n'
        f'      "headers": {{ "Authorization": "Bearer {_KEY}" }}\n'
        "    }\n  }\n}"
    )
    codex = f'[mcp_servers.tasks]\nurl = "{url}"\nbearer_token_env_var = "TASKS_API_KEY"'
    body = f"""<h1>Follow the work of your AI agents on your Apple devices.</h1>
<p class="lead">{name} gives Claude Code, Cursor, Codex, and other agents one place to report their work.
You follow each task live on iPhone, iPad, and Apple Watch.</p>
{store}

<h2>How it works</h2>
<ol>
<li>Open the app and make an API key. Give it a label, for example <code>Claude Code</code>.</li>
<li>Add this server to your agent with that key. The server speaks the Model Context Protocol (MCP).</li>
<li>The agent makes a task before it starts work, then keeps the status and the progress current.
Your devices show each change, send a notification, and show a Live Activity for work in progress.</li>
</ol>

<h2>Connect an agent</h2>
<p>Replace <code>{_KEY}</code> with a key from the app. The app shows a key one time only.</p>

<h3>Claude Code</h3>
{code(f'claude mcp add --transport http tasks {url} --header "Authorization: Bearer {_KEY}"')}

<h3>Cursor</h3>
<p class="dim">Put this in <code>~/.cursor/mcp.json</code>.</p>
{code(cursor)}

<h3>Codex</h3>
<p class="dim">Put this in <code>~/.codex/config.toml</code>, then set <code>TASKS_API_KEY</code> in your shell.</p>
{code(codex)}

<h3>Other MCP clients</h3>
<p>Use the Streamable HTTP transport with the URL <code>{escape(url)}</code> and the header
<code>Authorization: Bearer {_KEY}</code>. The header <code>X-API-Key</code> also works.
If a client cannot send headers, use <code>{escape(url)}?key={_KEY}</code>. A URL can appear in logs,
so use a header if you can.</p>

<h2>What an agent can do</h2>
<ul>
<li><code>create_task</code>, <code>list_tasks</code>, <code>get_task</code>, <code>update_task</code>,
<code>delete_task</code>: manage the tasks of its own key.</li>
<li><code>notify_user</code>: send you a notification when you must act or must know now.</li>
</ul>
<p>Each key sees only its own tasks. You see the tasks of all your keys.</p>"""
    return page(s.app_name, s.app_name, f"{s.app_name}: follow the work of your AI agents on Apple devices.", body)


def _privacy(s: Settings) -> str:
    name, email = escape(s.app_name), escape(s.support_email)
    days = CLOSED_TASK_TTL_SECONDS // 86400
    body = f"""<h1>Privacy</h1>
<p class="dim">Last updated: {_UPDATED}</p>
<p class="lead">{name} has no user accounts with names, no email addresses, and no passwords.
It does not track you and it has no ads.</p>

<h2>What this server stores</h2>
<ul>
<li><strong>An anonymous account ID.</strong> The app gets a random ID and a secret token the first time
it opens. The server keeps only a SHA-256 hash of the token.</li>
<li><strong>Your devices.</strong> For each device: a device ID that the app makes, the platform, the model,
the OS version, the app version, and the push tokens that Apple gives the app.</li>
<li><strong>Your API keys.</strong> For each key: a SHA-256 hash of the secret, your label, the first and
last characters of the secret so that you can recognize it, the time of the last use, and the name and
version of the last MCP client that used it. The server cannot show a secret again.</li>
<li><strong>Your tasks.</strong> The titles, descriptions, statuses, priorities, progress, and notes that
your agents or you write.</li>
</ul>

<h2>What this server does not store</h2>
<ul>
<li>No name, no email address, no phone number, no contacts, no location.</li>
<li>No analytics, no advertising identifiers, no tracking cookies. These pages set no cookies.</li>
</ul>

<h2>Who gets your data</h2>
<ul>
<li><strong>Apple.</strong> To send a notification or to update a Live Activity, the server sends the push
token and the content of the notification (for example a task title and a key label) to the Apple Push
Notification service.</li>
<li><strong>The hosting provider.</strong> The server and its database run at a hosting provider, which keeps
standard request logs. The server itself never writes a token or an API key to a log.</li>
</ul>
<p>The operator does not sell data and does not share it with any other party.</p>

<h2>Rate limits</h2>
<p>To stop abuse, the server counts requests for each credential, and counts new accounts for each IP
address. A counter for an IP address stays for two hours at most.</p>

<h2>How long data stays</h2>
<ul>
<li>A task with the status done or cancelled is deleted {days} days after its last change.</li>
<li>All other data stays until you delete it.</li>
</ul>

<h2>How to delete your data</h2>
<p>Delete a task, a key, or the full account in the app. A deleted key takes all its tasks with it.
A deleted account takes its devices, keys, and tasks with it, immediately. You can also send
<code>DELETE /v1/account</code> with your account token.</p>

<h2>Contact</h2>
<p><a href="mailto:{email}">{email}</a></p>"""
    return page(s.app_name, f"Privacy - {s.app_name}", f"What {s.app_name} stores and how to delete it.", body)


def _terms(s: Settings) -> str:
    name, email = escape(s.app_name), escape(s.support_email)
    body = f"""<h1>Terms</h1>
<p class="dim">Last updated: {_UPDATED}</p>
<p>These terms apply to this server and to the {name} app. If you use the service, you agree to them.</p>

<h2>The service</h2>
<p>{name} stores tasks that your AI agents and you create, shows them on your devices, and sends you
notifications about them. The service can change, and it can stop.</p>

<h2>Your keys and your content</h2>
<ul>
<li>You are responsible for your API keys and for what an agent does with them. Delete a key in the app
if it is lost.</li>
<li>Your tasks are yours. The operator uses them only to run the service.</li>
<li>Do not put secrets, such as passwords, into tasks. Task content goes to your devices in notifications.</li>
</ul>

<h2>Acceptable use</h2>
<ul>
<li>Do not use the service for unlawful content or to send notifications to a person who did not ask for them.</li>
<li>Do not try to get the data of other accounts, and do not go around the rate limits.</li>
</ul>
<p>The operator can limit or remove an account that breaks these rules.</p>

<h2>No warranty</h2>
<p>The service is given as it is, with no warranty. A notification can be late or can fail to arrive.
Do not rely on the service where a failure can cause damage or injury.</p>

<h2>Liability</h2>
<p>To the extent that the law permits, the operator is not liable for indirect or consequential loss,
or for loss of data.</p>

<h2>Changes</h2>
<p>These terms can change. The date at the top shows the last change.</p>

<h2>Contact</h2>
<p><a href="mailto:{email}">{email}</a></p>"""
    return page(s.app_name, f"Terms - {s.app_name}", f"Terms of use for {s.app_name}.", body)


def _support(s: Settings) -> str:
    name, email = escape(s.app_name), escape(s.support_email)
    body = f"""<h1>Support</h1>
<p class="lead">Write to <a href="mailto:{email}">{email}</a>. Do not send an API key in an email.</p>

<h2>My agent cannot connect</h2>
<ul>
<li>The URL is <code>{escape(s.mcp_url)}</code> and the transport is Streamable HTTP.</li>
<li>The header is <code>Authorization: Bearer mck_...</code>. A status <code>401</code> means that the key is
missing, wrong, or deleted.</li>
<li>The setup for each agent is on the <a href="/">start page</a>.</li>
</ul>

<h2>I lost an API key</h2>
<p>The server keeps only a hash of each key, so it cannot show the key again. Delete the key in the app
and make a new one. The tasks of a deleted key are deleted with it.</p>

<h2>I get no notifications</h2>
<ul>
<li>Allow notifications for {name} in the Settings app of the device.</li>
<li>A task that an agent made with <code>notify: false</code> updates silently.</li>
<li>A change of only the progress or the status note updates the device without an alert.</li>
</ul>

<h2>I want to delete my data</h2>
<p>Delete the account in the app. This removes the account, its devices, its keys, and its tasks
immediately. See <a href="/privacy">Privacy</a>.</p>"""
    return page(s.app_name, f"Support - {s.app_name}", f"Help for {s.app_name}.", body)


def pages_router(settings: Settings) -> APIRouter:
    router = APIRouter(include_in_schema=False)
    rendered = {
        "/": _home(settings),
        "/privacy": _privacy(settings),
        "/terms": _terms(settings),
        "/support": _support(settings),
    }

    def add(path: str, html: str) -> None:
        async def handler() -> HTMLResponse:
            return HTMLResponse(html, headers=_CACHE)

        router.add_api_route(path, handler, methods=["GET"])

    for path, html in rendered.items():
        add(path, html)
    return router
