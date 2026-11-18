"""dayfile.app as an OAuth 2.1 authorization server for assistants (MCP connectors).

A client registers itself (RFC 7591), sends the signed-in owner to /oauth/authorize, and trades the
resulting one-time code (with its PKCE verifier) at /oauth/token for a short-lived credential for
that owner's log plus a rotating refresh token. The credential is a read token, joined with a
write token if the owner allowed writing (see dayfile.mcp_http); both are ordinary tokens in the
owner's log, so they show up in Settings and can be revoked there.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from dayfile.ratelimit import Window
from dayfile.web.accounts import grants, store
from dayfile.web.accounts.control import Control

ACCESS_SECONDS = 3600
CONNECTED_NOTE = "connected app: "  # tokens with this note belong to a connection
RETURN = re.compile(r"/oauth/authorize\?[A-Za-z0-9._~%:/?&=+,\-]{1,1900}")
NO_CACHE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
MAX_REGISTRATION = 8192
KNOWN_HOSTS = (
    "claude.ai",
    "claude.com",
)  # where Anthropic's apps return; anything else is unverified


@dataclass
class Deps:
    """What the OAuth routes borrow from the web app."""

    site_origin: str
    base_domain: str
    with_db: Callable[[Callable[[Any], Any]], Any]
    clock: Callable[[], str]
    budgets: Window
    control: Control
    page: Callable[..., Response]
    message: Callable[..., Response]
    current: Callable[[Request], Any]
    same_origin: Callable[[Request], bool]
    return_cookie: str
    cookie_flags: dict[str, Any]
    register_limit: tuple[int, int] = (60, 3600)
    token_limit: tuple[int, int] = (600, 60)


def mint_pair(
    control: Control, slug: str, can_write: bool, client: str
) -> tuple[list[str], str] | None:
    """Mint the connection's tokens in the owner's log; return their ids and the joined secret."""
    note = (CONNECTED_NOTE + client)[:80]
    spec: dict[str, Any] = {"labels": ["self"], "note": note, "expires_in": ACCESS_SECONDS}
    read = control.mint(slug, {**spec, "kind": "read"})
    if read.status != 201 or not isinstance(read.body, dict):
        return None
    ids, secrets_ = [read.body["id"]], [read.body["token"]]
    if can_write:
        write = control.mint(slug, {**spec, "kind": "write", "write_source": "mcp"})
        if write.status != 201 or not isinstance(write.body, dict):
            control.revoke(slug, ids[0])
            return None
        ids.append(write.body["id"])
        secrets_.append(write.body["token"])
    return ids, "~".join(secrets_)


def revoke_all(control: Control, slug: str | None, token_ids: list[str]) -> None:
    """Revoke tokens a connection issued; the log may be gone by now, so failures are ignored."""
    for token_id in token_ids if slug else []:
        control.revoke(slug, token_id)


def oauth_error(status: int, error: str, description: str = "") -> JSONResponse:
    """An RFC 6749 error body."""
    body = {"error": error, **({"error_description": description} if description else {})}
    return JSONResponse(body, status_code=status, headers=NO_CACHE)


def bounce(redirect_uri: str, state: str, issuer: str, **params: str) -> Response:
    """Send the browser back to the client with `params`, the state and the issuer (RFC 9207)."""
    query = {**params, **({"state": state} if state else {}), "iss": issuer}
    parts = urlsplit(redirect_uri)
    joined = (parts.query + "&" if parts.query else "") + urlencode(query)
    target = parts._replace(query=joined).geturl()
    return RedirectResponse(target, status_code=303, headers=NO_CACHE)


def install(app: FastAPI, d: Deps) -> None:
    """Add the OAuth routes to the web app."""
    issuer = d.site_origin

    @app.get("/.well-known/oauth-authorization-server")
    async def server_metadata() -> Response:
        """RFC 8414 discovery document."""
        return JSONResponse(
            {
                "issuer": issuer,
                "authorization_endpoint": f"{issuer}/oauth/authorize",
                "token_endpoint": f"{issuer}/oauth/token",
                "registration_endpoint": f"{issuer}/oauth/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
                "scopes_supported": ["offline_access"],
                "authorization_response_iss_parameter_supported": True,
            },
            headers={"Cache-Control": "public, max-age=300"},
        )

    @app.post("/oauth/register")
    async def register(request: Request) -> Response:
        """Dynamic client registration (RFC 7591) for public clients."""
        if d.budgets.charge("oauth-register", 1, *d.register_limit):
            return oauth_error(429, "temporarily_unavailable", "too many registrations")
        raw = await request.body()
        try:
            doc = json.loads(raw) if raw and len(raw) <= MAX_REGISTRATION else None
        except ValueError:
            doc = None
        if not isinstance(doc, dict):
            return oauth_error(400, "invalid_client_metadata", "send a JSON object")
        if doc.get("token_endpoint_auth_method", "none") != "none":
            return oauth_error(400, "invalid_client_metadata", "only public clients are supported")
        now = d.clock()
        try:
            client = await run_in_threadpool(
                d.with_db,
                lambda c: grants.register_client(
                    c, now, doc.get("client_name"), doc.get("redirect_uris")
                ),
            )
        except grants.ClientError as e:
            return oauth_error(400, "invalid_redirect_uri", str(e))
        return JSONResponse(
            {
                "client_id": client["id"],
                "client_name": client["name"],
                "redirect_uris": client["redirect_uris"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
            status_code=201,
            headers=NO_CACHE,
        )

    def resource_url(slug: str) -> str:
        return f"https://{slug}.{d.base_domain}/mcp"

    def read_ask(values: Any, conn: Any) -> tuple[dict[str, Any], str] | str:
        """The registered client and redirect for a request, or the reason it cannot proceed."""
        client = grants.get_client(conn, str(values.get("client_id", "")))
        uri = str(values.get("redirect_uri", ""))
        if client is None:
            return "That application is not registered. Start the connection again from it."
        if not uri or not grants.redirect_matches(client, uri):
            return "That application sent an address it did not register, so nothing was shared."
        return client, uri

    def start_over(request: Request, destination: str) -> Response:
        """Send an anonymous visitor to sign in, and bring them back here afterwards."""
        response = RedirectResponse(destination, status_code=303)
        back = "/oauth/authorize?" + request.url.query
        if RETURN.fullmatch(back):
            response.set_cookie(d.return_cookie, back, max_age=600, **d.cookie_flags)
        return response

    async def ask_page(request: Request, values: Any) -> Response:
        """Validate an authorization request and show the consent screen."""
        who = await d.current(request)
        if who is None:
            return start_over(request, "/login?connect=1")
        if who["pending"]:
            return start_over(request, "/signup")
        found = await run_in_threadpool(d.with_db, lambda c: read_ask(values, c))
        if isinstance(found, str):
            return d.message(request, 400, "Can't connect", found)
        client, uri = found
        state = str(values.get("state", ""))[:500]
        failure = None
        if values.get("response_type") != "code":
            failure = ("unsupported_response_type", "only the authorization code flow")
        elif values.get("code_challenge_method") != "S256" or not grants.CHALLENGE.fullmatch(
            str(values.get("code_challenge", ""))
        ):
            failure = ("invalid_request", "PKCE with S256 is required")
        if failure:
            return bounce(uri, state, issuer, error=failure[0], error_description=failure[1])
        if not who["slug"]:
            return d.message(
                request,
                400,
                "Choose your username first",
                "Your log has no address yet. Choose a username on your account page, "
                "then connect again.",
            )
        wanted = str(values.get("resource", ""))
        if wanted and wanted != resource_url(who["slug"]):
            return d.message(
                request,
                400,
                "Wrong account",
                f"This connection is for {wanted}, which is not the log of the account you are "
                f"signed in as ({who['login']}). Sign out, then sign in with the right account.",
            )
        origin = urlsplit(uri)
        policy = (
            "default-src 'none'; style-src 'self'; img-src 'self'; font-src 'self'; "
            f"form-action 'self' {origin.scheme}://{origin.netloc}; base-uri 'none'; "
            "frame-ancestors 'none'"
        )
        return d.page(
            request,
            "oauth_consent.html",
            headers={"Content-Security-Policy": policy},
            client=client["name"],
            redirect_host=origin.netloc,
            known=origin.hostname in KNOWN_HOSTS,
            local=grants.is_loopback(origin.hostname),
            login=who["login"],
            slug=who["slug"],
            active="",
            fields={
                k: str(values.get(k, ""))[:2000]
                for k in ("client_id", "redirect_uri", "code_challenge", "state", "resource")
            },
        )

    @app.get("/oauth/authorize")
    async def authorize(request: Request) -> Response:
        """The page where an owner approves or refuses a connection."""
        return await ask_page(request, request.query_params)

    @app.post("/oauth/authorize")
    async def decide(
        request: Request,
        client_id: str = Form(""),
        redirect_uri: str = Form(""),
        code_challenge: str = Form(""),
        state: str = Form(""),
        resource: str = Form(""),
        decision: str = Form(""),
        access: str = Form("write"),
    ) -> Response:
        """Record the owner's answer and send them back to the application."""
        if not d.same_origin(request):
            return d.message(
                request, 403, "Not allowed", "That request did not come from this site."
            )
        values = {"client_id": client_id, "redirect_uri": redirect_uri, "resource": resource}
        who = await d.current(request)
        if who is None or who["pending"] or not who["slug"]:
            return RedirectResponse("/login", status_code=303)
        found = await run_in_threadpool(d.with_db, lambda c: read_ask(values, c))
        if isinstance(found, str):
            return d.message(request, 400, "Can't connect", found)
        _, uri = found
        state = state[:500]
        if decision != "allow":
            return bounce(uri, state, issuer, error="access_denied")
        if not grants.CHALLENGE.fullmatch(code_challenge) or (
            resource and resource != resource_url(who["slug"])
        ):
            return bounce(uri, state, issuer, error="invalid_request")
        now = d.clock()
        code = await run_in_threadpool(
            d.with_db,
            lambda c: grants.create_code(
                c, now, client_id, who["account_id"], uri, code_challenge, access == "write"
            ),
        )
        return bounce(uri, state, issuer, code=code)

    def tokens_for(
        account: dict[str, Any], client: dict[str, Any], can_write: bool
    ) -> tuple[list[str], str] | None:
        return mint_pair(d.control, account["slug"], can_write, client["name"])

    def answer(access: str, refresh: str) -> Response:
        return JSONResponse(
            {
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": ACCESS_SECONDS,
                "refresh_token": refresh,
            },
            headers=NO_CACHE,
        )

    @app.post("/oauth/token")
    async def token(
        grant_type: str = Form(""),
        client_id: str = Form(""),
        code: str = Form(""),
        redirect_uri: str = Form(""),
        code_verifier: str = Form(""),
        refresh_token: str = Form(""),
    ) -> Response:
        """Trade a code or a refresh token for a credential."""
        if d.budgets.charge("oauth-token", 1, *d.token_limit):
            return oauth_error(429, "temporarily_unavailable", "slow down")
        now = d.clock()
        client = await run_in_threadpool(d.with_db, lambda c: grants.get_client(c, client_id))
        if client is None:
            return oauth_error(401, "invalid_client")
        if grant_type == "authorization_code":
            return await from_code(now, client, code, redirect_uri, code_verifier)
        if grant_type == "refresh_token":
            return await from_refresh(now, client, refresh_token)
        return oauth_error(400, "unsupported_grant_type")

    def active_account(conn: Any, account_id: str) -> dict[str, Any] | None:
        account = store.get_account_by_id(conn, account_id)
        ok = account and account["status"] == "active" and account["slug"]
        return account if ok else None

    async def from_code(
        now: str, client: dict[str, Any], code: str, redirect_uri: str, verifier: str
    ) -> Response:
        redeemed = await run_in_threadpool(
            d.with_db,
            lambda c: grants.redeem_code(c, now, code, client["id"], redirect_uri, verifier),
        )
        if redeemed is None:
            return oauth_error(400, "invalid_grant")
        account = await run_in_threadpool(
            d.with_db, lambda c: active_account(c, redeemed["account_id"])
        )
        if account is None:
            return oauth_error(400, "invalid_grant")
        minted = await run_in_threadpool(tokens_for, account, client, redeemed["can_write"])
        if minted is None:
            return oauth_error(503, "temporarily_unavailable")
        ids, secret = minted
        _, refresh = await run_in_threadpool(
            d.with_db,
            lambda c: grants.create_grant(
                c, now, client["id"], account["id"], redeemed["can_write"], ids
            ),
        )
        return answer(secret, refresh)

    async def from_refresh(now: str, client: dict[str, Any], raw: str) -> Response:
        verdict, grant = await run_in_threadpool(
            d.with_db, lambda c: grants.find_by_refresh(c, now, raw, client["id"])
        )
        if grant is None:
            return oauth_error(400, "invalid_grant")
        account = await run_in_threadpool(
            d.with_db, lambda c: active_account(c, grant["account_id"])
        )
        if verdict == "reused" or account is None:
            if account is not None:
                await run_in_threadpool(revoke_all, d.control, account["slug"], grant["token_ids"])
            await run_in_threadpool(d.with_db, lambda c: grants.drop_grant(c, grant["id"]))
            return oauth_error(400, "invalid_grant")
        minted = await run_in_threadpool(tokens_for, account, client, bool(grant["can_write"]))
        if minted is None:
            return oauth_error(503, "temporarily_unavailable")
        ids, secret = minted
        refresh = await run_in_threadpool(
            d.with_db, lambda c: grants.rotate(c, now, grant, raw, ids)
        )
        if refresh is None:  # another request used this refresh token first
            await run_in_threadpool(revoke_all, d.control, account["slug"], ids)
            return oauth_error(400, "invalid_grant")
        await run_in_threadpool(revoke_all, d.control, account["slug"], grant["token_ids"])
        return answer(secret, refresh)
