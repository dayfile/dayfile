"""The dayfile.app web app: public pages, sign-in with a read token, and a read-only log viewer.

It holds no data and no tenant files. It reads a log through the public API with the token the
visitor gives it, and keeps that token only in a locked-down cookie.
"""

import asyncio
import hashlib
import hmac
import json
import os
import re
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from dayfile import timefmt
from dayfile.icons import ICONS, STATIC
from dayfile.ratelimit import Window
from dayfile.tenancy import SLUG
from dayfile.web import demo as sample
from dayfile.web import oauth, present, uptimerobot
from dayfile.web.accounts import control as control_mod
from dayfile.web.accounts import grants, store
from dayfile.web.accounts.github import GitHubError, GitHubOAuth
from dayfile.web.client import ApiResult, Fetch, Post, http_fetch, http_post, http_status
from dayfile.web.creds import TOKEN, parse_credentials, parse_single_token
from dayfile.web.history import INTERVAL, StatusHistory

HERE = Path(__file__).parent
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; "
    "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)
SESSION_SECONDS = 8 * 3600
VIEWER_NOTE = "dashboard viewer"  # short-lived tokens the dashboard makes for the log viewer
TOKEN_ID = re.compile(r"[0-9a-f]{12}")
TAG = re.compile(r"[a-z0-9][a-z0-9/_-]{0,63}")  # the schema's own tag pattern
ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
CLOSERS = {"close": "completes", "retract": "retracts"}
LIFECYCLE = ("completes", "supersedes", "retracts")
RELEASED = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
LIMITS = (50, 100, 200)
MAX_TAGS = 5
SECURITY_TXT_EXPIRES = "2027-11-12T00:00:00Z"  # renew yearly; a test warns 30 days before
STATUSES = {
    "": [],
    "open": ["open"],
    "closed": ["closed"],
    "none": ["none"],
    "retracted": ["retracted"],
    "all": ["open", "closed", "none", "superseded", "retracted"],
}


@dataclass
class Filters:
    """What the visitor asked to see, already checked."""

    tags: list[str] = field(default_factory=list)
    status: str = ""
    limit: int = LIMITS[0]
    errors: list[str] = field(default_factory=list)

    def api(self, limit: int | None = None) -> list[tuple[str, str]]:
        """The query for `GET /events`."""
        params = [("limit", str(limit or self.limit))]
        params += [("tag", t) for t in self.tags]
        params += [("status", s) for s in STATUSES[self.status]]
        return params

    def view(self) -> list[tuple[str, str]]:
        """The same filters as parameters of this app's own pages."""
        pairs = [("tag", " ".join(self.tags)), ("status", self.status)]
        return [(k, v) for k, v in pairs if v]


def parse_filters(query: Mapping[str, str]) -> Filters:
    """Read the viewer's filter parameters, ignoring (and reporting) any that are not valid."""
    f = Filters()
    tag, status = (query.get(k, "").strip() for k in ("tag", "status"))
    names = tag.replace(",", " ").split()
    if names and len(names) <= MAX_TAGS and all(TAG.fullmatch(n) for n in names):
        f.tags = list(dict.fromkeys(names))
    elif names:
        f.errors.append("tags")
    if status in STATUSES:
        f.status = status
    else:
        f.errors.append("status")
    limit = query.get("limit", "")
    f.limit = int(limit) if limit.isdigit() and int(limit) in LIMITS else LIMITS[0]
    return f


def fullness(usage: Any) -> str:
    """Return "full" at a plan limit, "near" from 90% of one, else an empty string."""
    if not isinstance(usage, dict):
        return ""
    worst = 0.0
    for used, cap in (("events", "max_events"), ("bytes", "max_bytes")):
        if usage.get(cap) and usage.get(used) is not None:
            worst = max(worst, usage[used] / usage[cap])
    return "full" if worst >= 1 else "near" if worst >= 0.9 else ""


def create_web_app(
    fetch: Fetch | None = None,
    base_domain: str | None = None,
    site_origin: str | None = None,
    secure_cookies: bool = True,
    login_limit: tuple[int, int] = (30, 60),
    monotonic: Callable[[], float] = time.monotonic,
    accounts_db: str | Path | None = None,
    github: Any = None,
    clock: Callable[[], str] = timefmt.now,
    control: control_mod.Control | None = None,
    post: Post | None = None,
    signup: str | None = None,
    discovery: bool | None = None,
    probe: Callable[[], str] | None = None,
    status_db: str | Path | None = None,
    probe_every: float = INTERVAL,
    uptime: Callable[[], Any] | None = None,
    single_log: bool | None = None,
) -> FastAPI:
    """Build the web app; the API address and domains default to the environment."""
    if single_log is None:  # one self-hosted log: no usernames, accounts or hosted pages
        single_log = os.environ.get("DAYFILE_SINGLE_LOG", "") == "1"
    if os.environ.get("DAYFILE_INSECURE_COOKIES", "") == "1":  # plain http on a home network
        secure_cookies = False
    base_domain = (base_domain or os.environ.get("DAYFILE_BASE_DOMAIN", "dayfile.cloud")).lower()
    site_origin = site_origin or os.environ.get("DAYFILE_SITE_ORIGIN", "https://dayfile.app")
    api_url = os.environ.get("DAYFILE_API_URL", "http://127.0.0.1:8000")
    accounts_db = accounts_db or os.environ.get("DAYFILE_ACCOUNTS_DB") or None
    github = github or github_from_environment(site_origin)
    if control is None:
        send = control_mod.from_environment(api_url, os.environ.get("DAYFILE_ADMIN_SECRET_FILE"))
        control = control_mod.Control(send) if send else None
    accounts_on = bool(accounts_db and github) and not single_log
    viewer_hook: Callable[[Request], Any] | None = None
    status_db = status_db or os.environ.get("DAYFILE_STATUS_DB")
    if not status_db and accounts_db:
        status_db = Path(accounts_db).with_name("status.db")
    history = StatusHistory(status_db) if status_db else None
    api_probe = probe or http_status(api_url)
    if uptime is None:
        robot_key = uptimerobot.read_key(os.environ.get("DAYFILE_UPTIMEROBOT_KEY_FILE"))
        if robot_key:
            show = frozenset(
                n.strip()
                for n in os.environ.get("DAYFILE_STATUS_MONITORS", "").split(",")
                if n.strip()
            )
            uptime = uptimerobot.Cached(
                lambda: uptimerobot.fetch(robot_key, int(time.time()), show)
            )
    status_page_url = os.environ.get("DAYFILE_STATUS_PAGE_URL", "")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Probe the API every minute for the status page's history, for as long as the app runs."""

        async def watch() -> None:
            while True:
                state = await run_in_threadpool(api_probe)
                if history:
                    await run_in_threadpool(history.record, clock(), state == "ok")
                await asyncio.sleep(probe_every)

        task = asyncio.create_task(watch()) if history else None
        try:
            yield
        finally:
            if task:
                task.cancel()

    app = FastAPI(
        title="dayfile.app", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.fetch = fetch or http_fetch(api_url, base_domain)
    app.state.post = post or http_post(api_url, base_domain)
    budgets = Window(monotonic)

    def nav_state(request: Request) -> dict[str, str]:
        """Which header link to show: the account bar only for a live, finished session."""
        if getattr(request.state, "signed_in", False):
            return {"nav_state": "account"}
        return {"nav_state": "log" if request.cookies.get(cookie) else ""}

    @app.middleware("http")
    async def mark_signed_in(request: Request, call_next: Callable[[Request], Any]) -> Response:
        """Check the session cookie once per page so a stale cookie never shows the account bar."""
        request.state.signed_in = False
        if (
            accounts_on
            and request.cookies.get(session_cookie)
            and not request.url.path.startswith("/static/")
        ):
            who = await current(request)
            request.state.signed_in = bool(who and not who["pending"])
        return await call_next(request)

    templates = Jinja2Templates(directory=HERE / "templates", context_processors=[nav_state])
    templates.env.filters.update(
        safe_href=present.safe_href,
        stamp=present.stamp,
        payload_text=present.payload_text,
        ago=lambda value: present.ago(value, clock()),
        markdown=present.markdown,
    )
    templates.env.globals["github_enabled"] = accounts_on
    templates.env.globals["single_log"] = single_log
    # Static files are cached for a week, so the page links carry a hash of the file they name.
    templates.env.globals["asset_v"] = {
        name: hashlib.sha256((HERE / "static" / name).read_bytes()).hexdigest()[:10]
        for name in ("site.css", "log.js", "tokens.js", "settings.js")
    }
    open_signup = (signup or os.environ.get("DAYFILE_SIGNUP", "invite")) == "open"
    templates.env.globals["open_signup"] = open_signup  # else an invite code is needed
    if discovery is None:  # hosted-only questions for the operator; off for self-hosting
        discovery = os.environ.get("DAYFILE_DISCOVERY", "") == "on"
    templates.env.globals["discovery"] = discovery
    templates.env.globals["email_consent"] = store.EMAIL_CONSENT
    templates.env.globals["base_domain"] = base_domain
    prefix = "__Host-" if secure_cookies else ""
    cookie, session_cookie, oauth_cookie, post_cookie, return_cookie = (
        prefix + "dayfile",
        prefix + "dayfile-session",
        prefix + "dayfile-oauth",
        prefix + "dayfile-post",
        prefix + "dayfile-return",
    )
    cookie_flags = {"path": "/", "secure": secure_cookies, "httponly": True, "samesite": "strict"}
    lax_flags = {**cookie_flags, "samesite": "lax"}  # sign-in returns from GitHub, a cross-site hop

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable[[Request], Any]) -> Response:
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=604800"  # a week
        else:
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    def page(
        request: Request,
        name: str,
        status: int = 200,
        public: bool = False,
        headers: dict[str, str] | None = None,
        **context: Any,
    ) -> HTMLResponse:
        # The header differs for signed-in visitors, so even public pages are for one browser only.
        cache = "private, max-age=60" if public else "no-store"
        return templates.TemplateResponse(
            request,
            name,
            context,
            status_code=status,
            headers={"Cache-Control": cache, "Vary": "Cookie", **(headers or {})},
        )

    def message(request: Request, status: int, title: str, text: str) -> HTMLResponse:
        return page(request, "error.html", status, title=title, text=text)

    def session(request: Request) -> tuple[str, str] | None:
        slug, _, token = request.cookies.get(cookie, "").partition(":")
        if SLUG.fullmatch(slug) and TOKEN.fullmatch(token):
            return slug, token
        return None

    def same_origin(request: Request) -> bool:
        origin = request.headers.get("origin")
        if origin == site_origin:
            return True
        if single_log and origin and origin != "null":  # no fixed public address: the page's own
            return urlsplit(origin).netloc == request.headers.get("host")
        if origin is None or origin == "null":
            # Browsers send "Origin: null" on form posts when the page's referrer policy is
            # no-referrer (ours is), so fall back to Fetch Metadata, which pages cannot forge.
            return request.headers.get("sec-fetch-site") == "same-origin"
        return False

    def signed_out(response: Response) -> Response:
        response.delete_cookie(cookie, **cookie_flags)
        response.delete_cookie(post_cookie, **cookie_flags)
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> HTMLResponse:
        if exc.status_code == 404:
            return message(request, 404, "Not found", "There is nothing at that address.")
        return message(request, exc.status_code, "Something went wrong", "Please try again.")

    # --- public pages ---------------------------------------------------------------

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def home(request: Request) -> Response:
        """The splash page for visitors; someone with a live session goes to their log instead.

        The session is checked, not just the cookie, so a stale cookie gets the splash page and
        never a detour through the sign-in page.
        """
        if single_log:
            return RedirectResponse(
                "/log" if request.cookies.get(cookie) else "/login",
                303,
                headers={"Cache-Control": "no-store", "Vary": "Cookie"},
            )
        if accounts_on and request.cookies.get(session_cookie):
            who = await current(request)
            if who and not who["pending"]:
                return RedirectResponse(
                    "/log" if who["slug"] else "/account",
                    303,
                    headers={"Cache-Control": "no-store", "Vary": "Cookie"},
                )
        return page(request, "index.html", public=True)

    @app.api_route("/.well-known/security.txt", methods=["GET", "HEAD"], include_in_schema=False)
    async def security_txt() -> Response:
        """How to report a security problem (RFC 9116)."""
        text = (
            "Contact: mailto:team@dayfile.app\n"
            f"Expires: {SECURITY_TXT_EXPIRES}\n"
            "Preferred-Languages: en\n"
            f"Canonical: {site_origin}/.well-known/security.txt\n"
        )
        return PlainTextResponse(text, headers={"Cache-Control": "public, max-age=86400"})

    @app.api_route("/demo", methods=["GET", "HEAD"], response_class=HTMLResponse)
    async def demo(request: Request) -> Response:
        """A read-only example log of sample events, for visitors who want to see it first."""
        if single_log:
            raise HTTPException(404)
        filters = parse_filters(request.query_params)
        events = sample.matching(sample.sample_events(timefmt.now()), filters.tags, filters.status)
        return page(
            request,
            "demo.html",
            events=events,
            filters=filters,
            statuses=["", "open", "closed", "none"],
        )

    for path, name in (
        ("/docs", "docs.html"),
        ("/terms", "terms.html"),
        ("/privacy", "privacy.html"),
    ):

        def public_page(request: Request, name: str = name) -> HTMLResponse:
            if single_log:  # these describe the hosted service, not your own server
                raise HTTPException(404)
            return page(request, name, public=True)

        app.add_api_route(path, public_page, methods=["GET", "HEAD"], include_in_schema=False)

    @app.api_route("/status", methods=["GET", "HEAD"], include_in_schema=False)
    async def status_page(request: Request) -> HTMLResponse:
        """A readable status: this site answered, and the API is asked just now."""
        if single_log:  # it describes the hosted service; a self-hosted server has /healthz
            raise HTTPException(404)
        api = await run_in_threadpool(api_probe)
        robot = await run_in_threadpool(uptime) if uptime else None
        return page(
            request,
            "status.html",
            200 if api == "ok" else 503,
            checked=clock(),
            parts=[("Website", "ok"), ("API and logs", api)],
            all_ok=api == "ok" and (robot is None or robot["all_up"]),
            robot=robot,
            status_page_url=status_page_url if status_page_url.startswith("https://") else "",
            history=await run_in_threadpool(history.summary, clock()) if history else None,
        )

    @app.api_route("/healthz", methods=["GET", "HEAD"])
    async def healthz() -> JSONResponse:
        """Liveness only; this app has no state to check."""
        return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})

    @app.api_route("/robots.txt", methods=["GET", "HEAD"])
    async def robots() -> PlainTextResponse:
        """Keep the signed-in pages out of search results."""
        return PlainTextResponse(
            "User-agent: *\n"
            + "".join(
                f"Disallow: {p}\n" for p in ("/log", "/login", "/auth", "/signup", "/account")
            ),
            headers={"Cache-Control": "public, max-age=3600"},
        )

    def icon_route(path: str, name: str, media_type: str) -> None:
        body = (STATIC / name).read_bytes()

        @app.api_route(path, methods=["GET", "HEAD"], include_in_schema=False)
        async def icon() -> Response:
            """The dayfile icon, the same file the API serves on tenant hosts."""
            return Response(
                body, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"}
            )

    for path, (name, media_type) in ICONS.items():
        icon_route(path, name, media_type)

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    # --- sign in and out ------------------------------------------------------------

    @app.api_route("/login", methods=["GET", "HEAD"], response_class=HTMLResponse)
    async def login_form(request: Request, expired: str = "", connect: str = "") -> Response:
        """The sign-in form; `?expired=1` explains why the visitor is back here.

        Someone who is already signed in goes to the log instead. `?expired=1` is exempt: it
        is where a dead session lands, and a log that cannot open must not bounce back here.
        """
        if expired != "1" and connect != "1":
            if getattr(request.state, "signed_in", False) or request.cookies.get(cookie):
                return RedirectResponse("/log", status_code=303)
        return page(
            request, "login.html", expired=expired == "1", connect=connect == "1", slug="", error=""
        )

    @app.post("/login")
    async def login(request: Request, slug: str = Form(""), token: str = Form("")) -> Response:
        """Check a read token against the API and, if it works, start a session."""
        if not same_origin(request):
            return message(request, 403, "Not allowed", "That request did not come from this site.")
        if budgets.charge("login", 1, *login_limit):
            error = "Too many attempts. Wait a minute and try again."
            return page(
                request, "login.html", 429, headers={"Retry-After": "60"}, error=error, slug=""
            )
        creds = (
            parse_single_token(token) if single_log else parse_credentials(slug, token, base_domain)
        )
        if creds is None:
            error = (
                "Enter a read token, or paste a feed URL."
                if single_log
                else "Enter your log name and a read token, or paste a feed URL."
            )
            return page(request, "login.html", 400, error=error, slug=slug.strip()[:63])
        result: ApiResult = await run_in_threadpool(
            request.app.state.fetch, creds[0], creds[1], [("limit", "1")]
        )
        if result.status == 200:
            response = RedirectResponse("/log", status_code=303)
            response.set_cookie(
                cookie, f"{creds[0]}:{creds[1]}", max_age=SESSION_SECONDS, **cookie_flags
            )
            return response
        mismatch = (
            "That token doesn't match." if single_log else "That log name and token don't match."
        )
        status, error = {
            401: (401, mismatch),
            404: (401, mismatch),
            403: (
                403,
                "That token can't read this log. Sign in with a read token; a write token "
                "can only post events.",
            ),
            429: (429, "This log is being read too fast. Slow down and try again shortly."),
        }.get(result.status, (503, "The log service isn't answering. Try again in a minute."))
        return page(request, "login.html", status, error=error, slug=creds[0])

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        """End the session."""
        if not same_origin(request):
            return message(request, 403, "Not allowed", "That request did not come from this site.")
        return signed_out(RedirectResponse("/", status_code=303))

    # --- accounts: sign in with GitHub, invite signup, the account page ----------------

    def with_db(work: Callable[[Any], Any]) -> Any:
        conn = store.connect(accounts_db)
        try:
            return work(conn)
        finally:
            conn.close()

    async def current(request: Request) -> dict[str, Any] | None:
        raw = request.cookies.get(session_cookie, "")
        if not raw:
            return None
        return await run_in_threadpool(with_db, lambda c: store.get_session(c, raw, clock()))

    def session_response(response: Response, raw: str) -> Response:
        response.set_cookie(session_cookie, raw, max_age=store.ABSOLUTE_SECONDS, **lax_flags)
        return response

    @app.api_route("/pricing", methods=["GET", "HEAD"], include_in_schema=False)
    async def pricing(request: Request) -> HTMLResponse:
        """The plans; with the discovery flag on, also the planned features people can ask about."""
        if single_log:
            raise HTTPException(404)
        mine: set[str] = set()
        signed_in = False
        home = "/log" if request.cookies.get(cookie) else ""
        if accounts_on and request.cookies.get(session_cookie):
            who = await current(request)
            if who and not who["pending"]:
                signed_in = True
                home = "/log" if who["slug"] else "/account"
                if discovery:
                    mine = await run_in_threadpool(
                        with_db, lambda c: store.my_interests(c, who["account_id"])
                    )
        q = request.query_params
        return page(
            request,
            "pricing.html",
            public=False,  # the page differs for someone signed in, so no shared caching
            home=home,
            features=store.FEATURES,
            mine=mine,
            signed_in=signed_in,
            thanks=q.get("thanks") == "1",
            saved=q.get("saved") == "1",
            error=q.get("error", "")[:200],
        )

    if accounts_on:

        @app.get("/auth/github")
        async def github_start(request: Request) -> Response:
            """Send the visitor to GitHub with a fresh state and PKCE challenge."""
            if budgets.charge("oauth", 1, *login_limit):
                return message(request, 429, "Slow down", "Too many sign-ins. Try again shortly.")
            state, verifier = await run_in_threadpool(
                with_db, lambda c: store.begin_oauth(c, clock())
            )
            response = RedirectResponse(github.authorize_url(state, verifier), status_code=302)
            response.set_cookie(oauth_cookie, state, max_age=store.OAUTH_SECONDS, **lax_flags)
            return response

        @app.get("/auth/github/callback")
        async def github_callback(
            request: Request, code: str = "", state: str = "", error: str = ""
        ) -> Response:
            """Finish sign-in: check the state, learn who GitHub says this is, start a session."""

            def done(response: Response) -> Response:
                response.delete_cookie(oauth_cookie, **lax_flags)
                return response

            expired = ("Sign-in did not finish", "That sign-in link expired. Start again.")
            sent = request.cookies.get(oauth_cookie, "")
            if error or not code or not state or len(state) > 100 or len(code) > 200:
                return done(message(request, 400, *expired))
            if not hmac.compare_digest(sent.encode(), state.encode()):
                return done(message(request, 400, *expired))
            verifier = await run_in_threadpool(
                with_db, lambda c: store.finish_oauth(c, state, clock())
            )
            if verifier is None:
                return done(message(request, 400, *expired))
            try:
                gid, login = await run_in_threadpool(github.identify, code, verifier)
            except GitHubError:
                return done(
                    message(request, 502, "GitHub didn't answer", "Try signing in again shortly.")
                )

            def start(conn: Any) -> tuple[str, bool] | None:
                now = clock()
                account = store.get_account(conn, github_id=gid)
                if account is None:
                    return store.create_session(conn, now, github=(gid, login)), False
                if account["status"] != "active":
                    return None
                store.refresh_login(conn, account["id"], login)
                return store.create_session(conn, now, account_id=account["id"]), True

            started = await run_in_threadpool(with_db, start)
            if started is None:
                return done(message(request, 403, "Can't sign in", "This account can't sign in."))
            raw, known = started
            back = request.cookies.get(return_cookie, "")
            after = back if oauth.RETURN.fullmatch(back) else "/account"
            response = page(request, "signed_in.html", next=after if known else "/signup")
            if known:
                response.delete_cookie(return_cookie, **lax_flags)
            return done(session_response(response, raw))

        @app.api_route("/signup", methods=["GET", "HEAD"], response_class=HTMLResponse)
        async def signup_form(request: Request) -> Response:
            """Ask a signed-in GitHub identity to accept the terms (and give a code, if needed)."""
            who = await current(request)
            if who is None:
                return RedirectResponse("/login", status_code=303)
            if not who["pending"]:
                return RedirectResponse("/account", status_code=303)
            return page(request, "signup.html", login=who["login"], error="")

        @app.post("/signup")
        async def signup(
            request: Request, invite: str = Form(""), terms: str = Form("")
        ) -> Response:
            """Create the account (with an invite code unless signup is open) and sign in as it."""
            if not same_origin(request):
                return message(
                    request, 403, "Not allowed", "That request did not come from this site."
                )
            who = await current(request)
            if who is None or not who["pending"]:
                return RedirectResponse("/login", status_code=303)

            def again(status: int, error: str) -> Response:
                return page(request, "signup.html", status, login=who["login"], error=error)

            if budgets.charge(("signup", who["github_id"]), 1, 5, 300) or budgets.charge(
                "signup", 1, 20, 60
            ):
                return again(429, "Too many tries. Wait a few minutes and try again.")
            if terms != "yes":
                return again(400, "Please accept the terms and privacy policy to continue.")
            if budgets.charge("signup-created", 1, 60, 3600):  # a brake on mass sign-ups
                return again(429, "Many people are signing up right now. Try again in a while.")

            def create(conn: Any) -> str:
                now = clock()
                account = store.create_account(
                    conn, None if open_signup else invite, who["github_id"], who["login"], now
                )
                store.accept_terms(conn, account["id"], now)
                old = request.cookies.get(session_cookie, "")
                store.end_session(conn, old)
                return store.create_session(conn, now, account_id=account["id"])

            try:
                raw = await run_in_threadpool(with_db, create)
            except store.InviteError:
                return again(400, "That invite code is not valid.")
            except store.AccountExists:
                return RedirectResponse("/login", status_code=303)
            back = request.cookies.get(return_cookie, "")
            after = back if oauth.RETURN.fullmatch(back) else "/account"
            return session_response(RedirectResponse(after, status_code=303), raw)

        if control is not None:
            oauth.install(
                app,
                oauth.Deps(
                    site_origin=site_origin,
                    base_domain=base_domain,
                    with_db=with_db,
                    clock=clock,
                    budgets=budgets,
                    control=control,
                    page=page,
                    message=message,
                    current=current,
                    same_origin=same_origin,
                    return_cookie=return_cookie,
                    cookie_flags=lax_flags,
                ),
            )

        async def owner(request: Request) -> dict[str, Any] | Response:
            """The signed-in account, or the redirect that sends the visitor where to sign in."""
            who = await current(request)
            if who is None:
                return RedirectResponse("/login", status_code=303)
            if who["pending"]:
                return RedirectResponse("/signup", status_code=303)
            return who

        def refused(request: Request) -> Response:
            return message(request, 403, "Not allowed", "That request did not come from this site.")

        def unavailable(request: Request) -> Response:
            return message(request, 503, "Unavailable", "Log management isn't available right now.")

        def api_error(result: ApiResult, fallback: str) -> str:
            body = result.body if isinstance(result.body, dict) else {}
            shown = str(body.get("error") or fallback)[:200]
            if "quota exceeded" in shown:
                return "Your log is full. Download it and start a fresh log in Settings."
            return shown

        def nav(who: dict[str, Any], active: str) -> dict[str, Any]:
            return {"login": who["login"], "slug": who["slug"], "active": active}

        @app.api_route("/account", methods=["GET", "HEAD"], response_class=HTMLResponse)
        async def account(request: Request, error: str = "") -> Response:
            """Home for the account: the log once it is named, else the form to name it."""
            who = await owner(request)
            if isinstance(who, Response):
                return who
            if who["slug"]:
                return RedirectResponse("/log", status_code=303)
            return page(request, "account.html", error=error[:200], **nav(who, ""))

        @app.api_route("/account/settings", methods=["GET", "HEAD"], response_class=HTMLResponse)
        async def settings(request: Request, error: str = "") -> Response:
            """Settings: the username, who you signed in as, and plan usage."""
            who = await owner(request)
            if isinstance(who, Response):
                return who
            if not who["slug"]:
                return RedirectResponse("/account", status_code=303)
            usage = None
            if control:
                result = await run_in_threadpool(control.tenant, who["slug"])
                usage = result.body if result.status == 200 else None
            answer, contact, details = await run_in_threadpool(
                with_db,
                lambda c: (
                    store.get_answer(c, who["account_id"]),
                    store.get_email(c, who["account_id"]),
                    store.account_export(c, who["account_id"]),
                ),
            )
            return page(
                request,
                "settings.html",
                usage=usage,
                account_json=json.dumps(details, indent=2, ensure_ascii=False),
                answer=answer,
                contact=contact,
                error=error[:200],
                **nav(who, "settings"),
            )

        @app.post("/pricing/interest")
        async def interest(
            request: Request,
            feature: Annotated[list[str], Form()] = [],  # noqa: B006
            email: str = Form(""),
            consent: str = Form(""),
        ) -> Response:
            """Roadmap interest in one request: signed in, the ticked set replaces yours."""
            if not same_origin(request):
                return refused(request)
            wanted = [f for f in dict.fromkeys(feature) if f in store.FEATURES]
            if not discovery:
                return RedirectResponse("/pricing", status_code=303)
            if budgets.charge("interest", 1, 30, 60):  # in memory, for everyone; no address is kept
                return message(
                    request, 429, "Slow down", "Too many requests. Try again in a minute."
                )
            who = await current(request)
            if who and not who["pending"]:
                await run_in_threadpool(
                    with_db, lambda c: store.set_interests(c, who["account_id"], wanted, clock())
                )
                return RedirectResponse("/pricing?saved=1#roadmap", status_code=303)
            address = email.strip()[:300]
            try:
                if not wanted:
                    raise ValueError("Tick at least one thing you would like to hear about.")
                if address and consent != "yes":
                    raise ValueError(
                        "Tick the box so we may email you, or leave the address empty."
                    )
                for item in wanted:
                    await run_in_threadpool(
                        with_db,
                        lambda c, f=item: store.add_interest(c, f, address or None, clock()),
                    )
            except ValueError as e:
                return RedirectResponse(
                    "/pricing?" + urlencode({"error": str(e)}) + "#roadmap", 303
                )
            return RedirectResponse("/pricing?thanks=1#roadmap", status_code=303)

        @app.post("/account/contact")
        async def save_contact(
            request: Request, email: str = Form(""), consent: str = Form(""), plans: str = Form("")
        ) -> Response:
            """Save the contact email and the plans answer together; a blank one is removed."""
            if not same_origin(request):
                return refused(request)
            who = await owner(request)
            if isinstance(who, Response):
                return who
            if not discovery:
                return RedirectResponse("/account/settings", status_code=303)
            address = email.strip()[:300]

            def work(conn: Any) -> None:
                if address:
                    store.clean_email(address)  # refuse a bad address before anything is written
                if len(store.clean_answer(plans)) > store.MAX_ANSWER:
                    raise ValueError(f"Keep it under {store.MAX_ANSWER} characters.")
                stored = store.get_email(conn, who["account_id"])
                if not address:
                    store.remove_email(conn, who["account_id"])
                elif not stored or stored["email"] != store.clean_email(address):
                    if consent != "yes":
                        raise ValueError("Tick the box so we may email you, or clear the email.")
                    store.set_email(conn, who["account_id"], address, clock())
                kept = store.get_answer(conn, who["account_id"])
                if (kept["text"] if kept else "") != store.clean_answer(plans):
                    store.set_answer(conn, who["account_id"], plans, clock())

            try:
                await run_in_threadpool(with_db, work)
            except ValueError as e:
                return RedirectResponse("/account/settings?" + urlencode({"error": str(e)}), 303)
            return RedirectResponse("/account/settings", status_code=303)

        @app.post("/account/answer")
        async def save_answer(request: Request, plans: str = Form("")) -> Response:
            """Change or remove the sign-up answer (a blank one removes it)."""
            if not same_origin(request):
                return refused(request)
            who = await owner(request)
            if isinstance(who, Response):
                return who
            if not discovery:
                plans = ""  # with the question off, an answer can only be removed
            try:
                await run_in_threadpool(
                    with_db, lambda c: store.set_answer(c, who["account_id"], plans, clock())
                )
            except ValueError as e:
                return RedirectResponse("/account/settings?" + urlencode({"error": str(e)}), 303)
            return RedirectResponse("/account/settings", status_code=303)

        @app.post("/account/email")
        async def save_email(
            request: Request, email: str = Form(""), consent: str = Form(""), remove: str = Form("")
        ) -> Response:
            """Change the contact address (with consent), or remove it and withdraw consent."""
            if not same_origin(request):
                return refused(request)
            who = await owner(request)
            if isinstance(who, Response):
                return who
            if remove or not discovery or not email.strip():
                await run_in_threadpool(with_db, lambda c: store.remove_email(c, who["account_id"]))
                return RedirectResponse("/account/settings", status_code=303)
            if consent != "yes":
                shown = "Tick the box so we may email you, or remove the address."
                return RedirectResponse("/account/settings?" + urlencode({"error": shown}), 303)
            try:
                await run_in_threadpool(
                    with_db,
                    lambda c: store.set_email(c, who["account_id"], email.strip()[:300], clock()),
                )
            except ValueError as e:
                return RedirectResponse("/account/settings?" + urlencode({"error": str(e)}), 303)
            return RedirectResponse("/account/settings", status_code=303)

        @app.post("/account/slug")
        async def claim_slug(
            request: Request,
            slug: str = Form(""),
            plans: str = Form(""),
            email: str = Form(""),
            consent: str = Form(""),
        ) -> Response:
            """Choose the log's name and create it, keeping the optional answer and email."""
            if not same_origin(request):
                return refused(request)
            who = await owner(request)
            if isinstance(who, Response):
                return who
            if control is None:
                return unavailable(request)
            slug = slug.strip().lower()

            plans = store.clean_answer(plans) if discovery else ""
            email = email.strip()[:300] if discovery else ""

            def again(status: int, error: str) -> Response:
                return page(
                    request,
                    "account.html",
                    status,
                    error=error,
                    plans=plans,
                    email=email,
                    consent=consent == "yes",
                    **nav(who, ""),
                )

            if len(plans) > store.MAX_ANSWER:
                return again(400, f"Keep your answer under {store.MAX_ANSWER} characters.")
            if email:
                if consent != "yes":
                    return again(400, "Tick the box so we may email you, or clear the email field.")
                try:
                    email = store.clean_email(email)
                except ValueError as e:
                    return again(400, str(e))
            if budgets.charge(("slug", who["account_id"]), 1, 10, 300):
                return again(429, "Too many tries. Wait a few minutes.")
            try:
                await run_in_threadpool(
                    with_db, lambda c: store.claim_slug(c, who["account_id"], slug, clock())
                )
            except store.SlugError as e:
                return again(400, str(e))
            made = await run_in_threadpool(control.create_tenant, slug)
            if made.status not in (200, 201):
                await run_in_threadpool(
                    with_db, lambda c: store.release_slug(c, who["account_id"], slug, clock())
                )
                return again(503, "Your log could not be created. Try again in a minute.")
            if plans:
                await run_in_threadpool(
                    with_db, lambda c: store.set_answer(c, who["account_id"], plans, clock())
                )
            if email:
                await run_in_threadpool(
                    with_db, lambda c: store.set_email(c, who["account_id"], email, clock())
                )
            return RedirectResponse("/account", status_code=303)

        @app.post("/account/rename")
        async def rename(
            request: Request, new_slug: str = Form(""), understood: str = Form("")
        ) -> Response:
            """Change the username (the subdomain); old addresses stop working at once."""
            if not same_origin(request):
                return refused(request)
            who = await owner(request)
            if isinstance(who, Response):
                return who
            if not who["slug"] or control is None:
                return RedirectResponse("/account", status_code=303)
            new, old = new_slug.strip().lower(), who["slug"]

            def again(status: int, error: str) -> Response:
                return page(
                    request,
                    "settings.html",
                    status,
                    usage=None,
                    error=error,
                    **nav(who, "settings"),
                )

            if understood != "yes":
                return again(400, "Please confirm that your old address will stop working.")
            if budgets.charge(("rename", who["account_id"]), 1, 10, 300):
                return again(429, "Too many tries. Wait a few minutes.")
            try:
                await run_in_threadpool(
                    with_db, lambda c: store.rename_slug(c, who["account_id"], new, clock())
                )
            except store.SlugError as e:
                return again(400, str(e))
            moved = await run_in_threadpool(control.rename, old, new)
            if moved.status != 200:
                await run_in_threadpool(
                    with_db, lambda c: store.undo_rename(c, who["account_id"], old, new, clock())
                )
                clash = moved.status == 409
                return again(
                    400 if clash else 503,
                    "That name is taken." if clash else "The rename failed. Nothing changed.",
                )
            return RedirectResponse("/account/settings", status_code=303)

        @app.api_route("/account/tokens", methods=["GET", "HEAD"], response_class=HTMLResponse)
        async def tokens_page(request: Request, error: str = "") -> Response:
            """Tokens: who or what may read or write this log."""
            who = await owner(request)
            if isinstance(who, Response):
                return who
            if not who["slug"] or control is None:
                return RedirectResponse("/account", status_code=303)
            result = await run_in_threadpool(control.tokens, who["slug"])
            if result.status != 200 or not isinstance(result.body, list):
                return unavailable(request)
            shown = [
                x
                for x in result.body
                if x.get("note") != VIEWER_NOTE
                and not (x.get("note") or "").startswith(oauth.CONNECTED_NOTE)
                and not x["revoked_at"]
            ]
            connections = await run_in_threadpool(
                with_db, lambda c: grants.list_grants(c, who["account_id"])
            )
            return page(
                request,
                "tokens.html",
                tokens=shown,
                connections=connections,
                error=error[:200],
                **nav(who, "tokens"),
            )

        @app.post("/account/tokens")
        async def tokens_create(
            request: Request,
            kind: str = Form("read"),
            labels: str = Form("self"),
            source: str = Form(""),
            expires: str = Form("365"),
            note: str = Form(""),
        ) -> Response:
            """Mint a token and show it once."""
            if not same_origin(request):
                return refused(request)
            who = await owner(request)
            if isinstance(who, Response):
                return who
            if not who["slug"] or control is None:
                return RedirectResponse("/account", status_code=303)
            spec: dict[str, Any] = {
                "kind": kind,
                "labels": [x.strip() for x in labels.split(",") if x.strip()] or ["self"],
                "note": note.strip()[:80] or None,
            }
            if kind == "write":
                spec["write_source"] = (source.strip() or who["slug"])[:100]
            if expires.isdigit() and int(expires) > 0:
                spec["expires_in"] = min(int(expires), 366) * 86400
            result = await run_in_threadpool(control.mint, who["slug"], spec)
            if result.status != 201 or not isinstance(result.body, dict):
                shown = api_error(result, "The token could not be created.")
                return RedirectResponse("/account/tokens?" + urlencode({"error": shown}), 303)
            return page(
                request, "token_new.html", token=result.body, spec=spec, **nav(who, "tokens")
            )

        @app.api_route(
            "/account/tokens/{token_id}/revoke",
            methods=["GET", "HEAD"],
            response_class=HTMLResponse,
        )
        async def revoke_confirm(request: Request, token_id: str) -> Response:
            """Ask before revoking."""
            who = await owner(request)
            if isinstance(who, Response):
                return who
            if not TOKEN_ID.fullmatch(token_id):
                return message(request, 404, "Not found", "There is nothing at that address.")
            return page(request, "revoke.html", token_id=token_id, **nav(who, "tokens"))

        @app.post("/account/tokens/{token_id}/revoke")
        async def revoke(request: Request, token_id: str) -> Response:
            """Revoke a token; anything using it stops working at once."""
            if not same_origin(request):
                return refused(request)
            who = await owner(request)
            if isinstance(who, Response):
                return who
            if not who["slug"] or control is None or not TOKEN_ID.fullmatch(token_id):
                return RedirectResponse("/account/tokens", status_code=303)
            await run_in_threadpool(control.revoke, who["slug"], token_id)
            return RedirectResponse("/account/tokens", status_code=303)

        @app.post("/account/connections/{grant_id}/disconnect")
        async def disconnect(request: Request, grant_id: str) -> Response:
            """End an assistant's connection; its tokens stop working at once."""
            if not same_origin(request):
                return refused(request)
            who = await owner(request)
            if isinstance(who, Response):
                return who
            if not who["slug"] or control is None:
                return RedirectResponse("/account", status_code=303)
            ids = await run_in_threadpool(
                with_db, lambda c: grants.delete_grant(c, who["account_id"], grant_id)
            )
            await run_in_threadpool(oauth.revoke_all, control, who["slug"], ids or [])
            return RedirectResponse("/account/tokens", status_code=303)

        @app.api_route("/account/feeds", methods=["GET", "HEAD"], response_class=HTMLResponse)
        async def feeds_page(request: Request, error: str = "", edit: str = "") -> Response:
            """Feeds: saved filters, each private or public."""
            who = await owner(request)
            if isinstance(who, Response):
                return who
            if not who["slug"] or control is None:
                return RedirectResponse("/account", status_code=303)
            result = await run_in_threadpool(control.feeds, who["slug"])
            if result.status != 200 or not isinstance(result.body, list):
                return unavailable(request)
            editing = next((f for f in result.body if f["name"] == edit), None)
            return page(
                request,
                "feeds.html",
                tab="feeds",
                feeds=result.body,
                editing=editing,
                error=error[:200],
                **nav(who, "feeds"),
            )

        @app.post("/account/feeds")
        async def feeds_save(
            request: Request,
            name: str = Form(""),
            title: str = Form(""),
            tags: str = Form(""),
            status: str = Form(""),
            within: str = Form(""),
            visibility: str = Form("private"),
        ) -> Response:
            """Create a feed or change one."""
            if not same_origin(request):
                return refused(request)
            who = await owner(request)
            if isinstance(who, Response):
                return who
            if not who["slug"] or control is None:
                return RedirectResponse("/account", status_code=303)
            name = name.strip().lower()
            spec: dict[str, Any] = {"title": title.strip()[:80], "visibility": visibility}
            if name != "default":
                condition = {
                    "tag": [x.strip() for x in tags.split(",") if x.strip()],
                    "status": [status] if status else [],
                    "within": within.strip(),
                }
                spec["filter"] = {k: v for k, v in condition.items() if v}
            result = await run_in_threadpool(control.save_feed, who["slug"], name, spec)
            if result.status != 200:
                shown = api_error(result, "The feed could not be saved.")
                return RedirectResponse("/account/feeds?" + urlencode({"error": shown}), 303)
            return RedirectResponse("/account/feeds", status_code=303)

        @app.post("/account/feeds/{name}/delete")
        async def feeds_delete(request: Request, name: str) -> Response:
            """Remove a feed; its address then answers 404."""
            if not same_origin(request):
                return refused(request)
            who = await owner(request)
            if isinstance(who, Response):
                return who
            if who["slug"] and control is not None:
                await run_in_threadpool(control.delete_feed, who["slug"], name)
            return RedirectResponse("/account/feeds", status_code=303)

        @app.api_route(
            "/account/feeds/heartbeats", methods=["GET", "HEAD"], response_class=HTMLResponse
        )
        async def heartbeats_page(request: Request, error: str = "") -> Response:
            """Heartbeats: how often each source or tag should report in."""
            who = await owner(request)
            if isinstance(who, Response):
                return who
            if not who["slug"] or control is None:
                return RedirectResponse("/account", status_code=303)
            beats = await run_in_threadpool(control.heartbeats, who["slug"])
            if beats.status != 200 or not isinstance(beats.body, list):
                return unavailable(request)
            return page(
                request,
                "heartbeats.html",
                tab="heartbeats",
                heartbeats=beats.body,
                error=error[:200],
                **nav(who, "feeds"),
            )

        @app.post("/account/heartbeats")
        async def heartbeats_save(
            request: Request,
            name: str = Form(""),
            watch: str = Form("source"),
            value: str = Form(""),
            every: str = Form(""),
        ) -> Response:
            """Create or replace a heartbeat."""
            if not same_origin(request):
                return refused(request)
            who = await owner(request)
            if isinstance(who, Response):
                return who
            if not who["slug"] or control is None:
                return RedirectResponse("/account", status_code=303)
            spec = {"every": every.strip(), ("tag" if watch == "tag" else "source"): value.strip()}
            result = await run_in_threadpool(
                control.save_heartbeat, who["slug"], name.strip().lower(), spec
            )
            if result.status != 200:
                shown = api_error(result, "The heartbeat could not be saved.")
                return RedirectResponse(
                    "/account/feeds/heartbeats?" + urlencode({"error": shown}), 303
                )
            return RedirectResponse("/account/feeds/heartbeats", status_code=303)

        @app.post("/account/heartbeats/{name}/delete")
        async def heartbeats_delete(request: Request, name: str) -> Response:
            """Remove a heartbeat; any missed-heartbeat to-do it left open is closed."""
            if not same_origin(request):
                return refused(request)
            who = await owner(request)
            if isinstance(who, Response):
                return who
            if who["slug"] and control is not None:
                await run_in_threadpool(control.delete_heartbeat, who["slug"], name)
            return RedirectResponse("/account/feeds/heartbeats", status_code=303)

        async def viewer_redirect(request: Request, who: dict[str, Any]) -> Response:
            """Mint a short-lived read token for this session and go to the log with it."""
            spec = {"kind": "read", "labels": ["self"], "expires_in": 3600, "note": VIEWER_NOTE}
            result = await run_in_threadpool(control.mint, who["slug"], spec)
            if result.status != 201 or not isinstance(result.body, dict):
                return unavailable(request)
            response = RedirectResponse("/log?fresh=1", status_code=303)
            response.set_cookie(
                cookie, f"{who['slug']}:{result.body['token']}", max_age=3600, **cookie_flags
            )
            return response

        async def open_viewer(request: Request) -> Response | None:
            """For an account session with no log cookie, start one; None if it cannot."""
            if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
                return None
            who = await current(request)
            if who is None or who["pending"] or not who["slug"] or control is None:
                return None
            return await viewer_redirect(request, who)

        viewer_hook = open_viewer

        @app.post("/account/view")
        async def view_log(request: Request) -> Response:
            """Open the log viewer with a short-lived read token made for this session."""
            if not same_origin(request):
                return refused(request)
            who = await owner(request)
            if isinstance(who, Response):
                return who
            if not who["slug"] or control is None:
                return RedirectResponse("/account", status_code=303)
            return await viewer_redirect(request, who)

        async def send_event(
            request: Request, slug: str, event: dict[str, Any]
        ) -> tuple[ApiResult, str] | Response:
            """Write one event as this session, with a short-lived `web` write token in a cookie."""
            held = request.cookies.get(post_cookie, "")
            token = held.partition(":")[2] if held.partition(":")[0] == slug else ""
            minted = False
            for _ in range(2):
                if not token:
                    spec = {
                        "kind": "write",
                        "labels": ["self"],
                        "write_source": "web",
                        "expires_in": 3600,
                        "note": VIEWER_NOTE,
                    }
                    made = await run_in_threadpool(control.mint, slug, spec)
                    if made.status != 201 or not isinstance(made.body, dict):
                        return unavailable(request)
                    token, minted = made.body["token"], True
                result = await run_in_threadpool(request.app.state.post, slug, token, event)
                if result.status not in (401, 403) or minted:
                    break
                token = ""  # the held token lapsed: make a fresh one and try once more
            return result, token

        @app.api_route("/log/post", methods=["GET", "HEAD"], response_class=HTMLResponse)
        async def post_form(request: Request) -> Response:
            """The page for writing a new event."""
            who = await owner(request)
            if isinstance(who, Response):
                return who
            if not who["slug"]:
                return RedirectResponse("/account", status_code=303)
            return page(request, "post.html", form={}, error="", **nav(who, "log"))

        @app.post("/log/post")
        async def log_post(
            request: Request,
            title: str = Form(""),
            body: str = Form(""),
            tags: str = Form(""),
        ) -> Response:
            """Post an event from the log page, with a short-lived write token for this session."""
            if not same_origin(request):
                return refused(request)
            who = await owner(request)
            if isinstance(who, Response):
                return who
            if not who["slug"] or control is None:
                return RedirectResponse("/account", status_code=303)
            slug = who["slug"]
            event: dict[str, Any] = {"title": title.strip()[:300]}
            form = {"title": title, "body": body, "tags": tags}

            def again(status: int, error: str) -> Response:
                return page(request, "post.html", status, form=form, error=error, **nav(who, "log"))

            if body.strip():
                event["body"] = body.strip()[:20000]
            if names := [t.strip().lower() for t in tags.replace(",", " ").split() if t.strip()]:
                event["tags"] = names[:20]
            if not event["title"]:
                return again(400, "Give it a title.")
            sent = await send_event(request, slug, event)
            if isinstance(sent, Response):
                return sent
            result, token = sent
            if result.status not in (200, 201):
                return again(
                    422 if result.status == 422 else 503,
                    api_error(result, "The event could not be posted."),
                )
            response = RedirectResponse("/log", status_code=303)
            response.set_cookie(post_cookie, f"{slug}:{token}", max_age=3600, **cookie_flags)
            return response

        @app.post("/log/event/{event_id}/{action}")
        async def close_event(
            request: Request, event_id: str, action: str, title: str = Form("")
        ) -> Response:
            """Close an event or retract it by posting a new event that refers back to it."""
            if action not in CLOSERS or not ULID.fullmatch(event_id):
                raise HTTPException(404)
            if not same_origin(request):
                return refused(request)
            who = await owner(request)
            if isinstance(who, Response):
                return who
            if not who["slug"] or control is None:
                return RedirectResponse("/account", status_code=303)
            event = {"refs": [{"rel": CLOSERS[action], "id": event_id}]}  # the title is inherited
            sent = await send_event(request, who["slug"], event)
            if isinstance(sent, Response):
                return sent
            result, token = sent
            if result.status not in (200, 201):
                shown = api_error(result, "That could not be saved.")
                return RedirectResponse("/log?" + urlencode({"error": shown}), 303)
            response = RedirectResponse("/log", status_code=303)
            response.set_cookie(post_cookie, f"{who['slug']}:{token}", max_age=3600, **cookie_flags)
            return response

        @app.post("/account/export")
        async def export_log(request: Request) -> Response:
            """Download a copy of the whole log as a SQLite database (gzipped)."""
            if not same_origin(request):
                return refused(request)
            who = await owner(request)
            if isinstance(who, Response):
                return who
            if not who["slug"] or control is None:
                return RedirectResponse("/account", status_code=303)
            if budgets.charge(("export", who["account_id"]), 1, 5, 300):
                shown = "Too many exports. Wait a few minutes."
                return RedirectResponse("/account/settings?" + urlencode({"error": shown}), 303)
            result = await run_in_threadpool(control.export, who["slug"])
            if result.status != 200 or not isinstance(result.body, bytes):
                shown = "The export could not be made. Try again in a minute."
                return RedirectResponse("/account/settings?" + urlencode({"error": shown}), 303)
            return Response(
                result.body,
                media_type="application/gzip",
                headers={
                    "Content-Disposition": f'attachment; filename="{who["slug"]}-dayfile.db.gz"',
                    "Cache-Control": "no-store",
                },
            )

        @app.post("/account/reset")
        async def reset_log(request: Request, confirm: str = Form("")) -> Response:
            """Erase the log's events, tokens and feeds and start it again, empty, as new."""
            if not same_origin(request):
                return refused(request)
            who = await owner(request)
            if isinstance(who, Response):
                return who
            slug = who["slug"]
            if not slug or control is None:
                return RedirectResponse("/account", status_code=303)
            if confirm.strip().lower() != slug:
                shown = "Type your username exactly to confirm."
                return RedirectResponse("/account/settings?" + urlencode({"error": shown}), 303)
            if budgets.charge(("reset", who["account_id"]), 1, 3, 300):
                shown = "Too many tries. Wait a few minutes."
                return RedirectResponse("/account/settings?" + urlencode({"error": shown}), 303)
            done = await run_in_threadpool(control.reset, slug)
            if done.status not in (200, 201):
                shown = "Your log could not be reset. Try again in a minute."
                return RedirectResponse("/account/settings?" + urlencode({"error": shown}), 303)
            response = RedirectResponse("/log", status_code=303)
            for name in (cookie, post_cookie):  # their tokens died with the old log
                response.delete_cookie(name, **cookie_flags)
            return response

        @app.post("/account/delete")
        async def delete_account(request: Request, confirm: str = Form("")) -> Response:
            """Delete the account and its log; the person types the username to confirm."""
            if not same_origin(request):
                return refused(request)
            who = await owner(request)
            if isinstance(who, Response):
                return who
            slug = who["slug"]
            if control is None:
                return unavailable(request)

            def again(status: int, error: str) -> Response:
                return page(
                    request,
                    "settings.html",
                    status,
                    usage=None,
                    error=error,
                    **nav(who, "settings"),
                )

            if slug and confirm.strip().lower() != slug:
                return again(400, "Type your username exactly to confirm.")
            if budgets.charge(("delete", who["account_id"]), 1, 5, 300):
                return again(429, "Too many tries. Wait a few minutes.")
            if slug:
                gone = await run_in_threadpool(control.delete_tenant, slug)
                if gone.status not in (204, 404):  # 404: an earlier try already moved it
                    return again(503, "Your log could not be deleted. Nothing changed; try again.")
            await run_in_threadpool(
                with_db, lambda c: store.delete_account(c, who["account_id"], clock())
            )
            response = page(request, "deleted.html", slug=slug or "")
            for name in (session_cookie, cookie, post_cookie):
                response.delete_cookie(
                    name, **(lax_flags if name == session_cookie else cookie_flags)
                )
            return response

        @app.post("/account/logout")
        async def account_logout(request: Request) -> Response:
            """Sign out of the account session."""
            if not same_origin(request):
                return message(
                    request, 403, "Not allowed", "That request did not come from this site."
                )
            raw = request.cookies.get(session_cookie, "")
            if raw:
                await run_in_threadpool(with_db, lambda c: store.end_session(c, raw))
            response = RedirectResponse("/", status_code=303)
            response.delete_cookie(session_cookie, **lax_flags)
            response.delete_cookie(cookie, **cookie_flags)  # the log viewer's token as well
            response.delete_cookie(post_cookie, **cookie_flags)
            return response

    # --- the log viewer -------------------------------------------------------------

    def cards(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Newest first, without the events that only close, replace or retract another."""
        shown = [
            e
            for e in events
            if not any(
                r["rel"] in CLOSERS.values() or r["rel"] == "supersedes" for r in e.get("refs", [])
            )
        ]
        return shown[::-1]

    async def history_of(
        request: Request, slug: str, token: str, shown: list[dict[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        """For each shown event, the chain of events that closed, replaced or retracted it."""
        if not shown:
            return {}
        params = [("since_seq", str(min(e["seq"] for e in shown))), ("limit", "200")]
        params += [("status", s) for s in STATUSES["all"]]
        result = await run_in_threadpool(request.app.state.fetch, slug, token, params)
        if result.status != 200 or not isinstance(result.body, dict):
            return {}
        by_target: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for event in result.body["events"]:
            for ref in event.get("refs", []):
                if ref["rel"] in LIFECYCLE:
                    by_target.setdefault(ref["id"], []).append((ref["rel"], event))

        def chain(event_id: str, depth: int = 0) -> list[dict[str, Any]]:
            if depth > 6:
                return []
            return [
                {
                    "event": c,
                    "rel": rel,
                    "live": c.get("status") != "retracted",
                    "children": chain(c["id"], depth + 1),
                }
                for rel, c in by_target.get(event_id, [])
            ]

        return {e["id"]: chain(e["id"]) for e in shown if e["id"] in by_target}

    def cursors(body: Any, filters: Filters) -> dict[str, Any]:
        query = [("after", body["cursor"]), ("released", body["released_cursor"])]
        return {"poll_url": "/log/new?" + urlencode(query + filters.view())}

    async def usable_write_tokens(slug: str) -> list[dict[str, Any]]:
        """The account's live write tokens, for the empty log's "use one you have" step."""
        result = await run_in_threadpool(control.tokens, slug)
        if result.status != 200 or not isinstance(result.body, list):
            return []
        return [
            x
            for x in result.body
            if x.get("kind") == "write"
            and not x["revoked_at"]
            and x.get("note") != VIEWER_NOTE
            and not (x.get("note") or "").startswith(oauth.CONNECTED_NOTE)
        ]

    @app.get("/log", response_class=HTMLResponse)
    async def log(request: Request, error: str = "") -> Response:
        """The newest events, newest first, with filters."""
        who = session(request)
        retry = (
            request.query_params.get("fresh") != "1"
        )  # a just-made viewer token failing is final
        if who is None:
            if viewer_hook and retry and request.cookies.get(session_cookie):
                opened = await viewer_hook(request)
                if opened is not None:
                    return opened
            return RedirectResponse(
                "/account" if request.cookies.get(session_cookie) else "/login", status_code=303
            )
        filters = parse_filters(request.query_params)
        result: ApiResult = await run_in_threadpool(
            request.app.state.fetch, who[0], who[1], filters.api()
        )
        if result.status in (401, 403, 404):
            if retry and request.cookies.get(session_cookie):  # the viewer token lapsed; remake it
                return signed_out(RedirectResponse("/log", status_code=303))
            return signed_out(RedirectResponse("/login?expired=1", status_code=303))
        if result.status == 429:
            return message(
                request, 429, "Slow down", "This log is being read too fast. Slow down and reload."
            )
        if result.status != 200 or not isinstance(result.body, dict):
            error = (result.body or {}).get("error") if result.status == 400 else None
            if isinstance(error, str):
                return message(request, 400, "That filter didn't work", error)
            return message(
                request,
                503,
                "Unavailable",
                "The log service isn't answering. Try again in a minute.",
            )
        events = cards(result.body["events"])
        capacity = ""
        write_tokens: list[dict[str, Any]] = []
        if control and request.cookies.get(session_cookie):
            acct = await current(request)
            if acct and acct["slug"]:
                used = await run_in_threadpool(control.tenant, acct["slug"])
                capacity = fullness(used.body) if used.status == 200 else ""
                if not events and not (filters.tags or filters.status):
                    write_tokens = await usable_write_tokens(acct["slug"])
        return page(
            request,
            "log.html",
            capacity=capacity,
            write_tokens=write_tokens,
            slug=who[0],
            active="log",
            history=await history_of(request, who[0], who[1], events),
            error=error[:200],
            events=events,
            filters=filters,
            limits=LIMITS,
            statuses=list(STATUSES),
            **cursors(result.body, filters),
        )

    def lapsed_poll(request: Request) -> Response:
        """A poll whose read session is gone: an account goes back to the log, which opens a new
        viewer; anyone else is sent to sign in."""
        again = "/log" if getattr(request.state, "signed_in", False) else "/login?expired=1"
        return signed_out(Response(status_code=401, headers={"HX-Redirect": again}))

    @app.get("/log/new", response_class=HTMLResponse)
    async def log_new(request: Request, after: str = "", released: str = "") -> Response:
        """What arrived since the page's cursors, as a fragment the page swaps in (htmx)."""
        who = session(request)
        if who is None:
            return lapsed_poll(request)
        if not (after.isdigit() and len(after) <= 12 and RELEASED.fullmatch(released)):
            return PlainTextResponse("bad cursor", status_code=400)
        filters = parse_filters(request.query_params)
        params = [("since_seq", after), ("released_after", released), *filters.api(limit=200)]
        result: ApiResult = await run_in_threadpool(request.app.state.fetch, who[0], who[1], params)
        if result.status in (401, 403, 404):
            return lapsed_poll(request)
        if result.status == 200 and isinstance(result.body, dict):
            events, body = cards(result.body["events"]), result.body
        else:  # keep polling from the same place
            events, body = [], {"cursor": after, "released_cursor": released}
        return page(request, "_new.html", events=events, **cursors(body, filters))

    return app


def github_from_environment(site_origin: str) -> GitHubOAuth | None:
    """Build the GitHub client from $GITHUB_CLIENT_ID and the file $DAYFILE_GITHUB_SECRET_FILE."""
    client_id = os.environ.get("GITHUB_CLIENT_ID", "").strip()
    path = os.environ.get("DAYFILE_GITHUB_SECRET_FILE", "")
    try:
        secret = Path(path).read_text().strip() if path else ""
    except OSError:
        secret = ""
    if not (client_id and secret):
        return None
    return GitHubOAuth(client_id, secret, f"{site_origin}/auth/github/callback")
