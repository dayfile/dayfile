# A review account for the Connectors Directory

What Anthropic's reviewers need, and the exact steps to set it up. Nothing here puts a credential in
this repository.

## When it is needed

- A listing is **Community** by default and needs no test account. The submission is scanned
  automatically.
- Some submissions are escalated to **Verified** review. A reviewer then emails the contact you gave
  and asks for a way to sign in. Anthropic's pages say not to put credentials in the portal, and they
  say nothing about how to send them. Reply to that email.
- Anthropic's pages also say nothing about two-factor sign-in, email verification codes, or sign-in
  through GitHub. **Before you submit, ask `mcp-review@anthropic.com` what they accept** (see step 4).

## Steps

1. **Make a GitHub account just for reviewers.** Use a name such as `dayfile-reviewer`. Give it an
   email address you can read: GitHub emails a code the first time an account signs in from a new
   device, and that code is what is most likely to block a reviewer.
2. **Create a dayfile account from it.** Open <https://dayfile.app/login> in a private window and
   sign in with that GitHub account. The first sign-in leads to sign-up; choose a username such as
   `reviewer`. While sign-up is by invite only, make a code on the server:
   `cd ~/dayfile/deploy/cloud && sudo docker compose exec -T web python -m dayfile.web.admin invite create --note reviewer`.
3. **Fill its log with sample events.** One command writes a week of backups and deploys, open and
   closed to-dos and one to-do from a missed heartbeat. It is safe to run again; it adds nothing the
   second time.
   ```
   ssh dayfile@<server> 'cd ~/dayfile/deploy/cloud && \
     sudo docker compose exec -T dayfile dayfile gen review-log \
       --db /data/tenants/reviewer/dayfile.db'
   ```
   Replace `reviewer` with the username. It prints how many events it wrote (19 the first time).
4. **Decide how reviewers sign in.** Email `mcp-review@anthropic.com`, say sign-in is with GitHub,
   and ask which of these they accept:
   - A GitHub account with **no two-factor** sign-in. GitHub may still ask for an emailed code on a
     new device, so the account's email must be one you can read and forward from quickly.
   - A GitHub account **with** two-factor sign-in, where you give the reviewer a one-time code
     when they ask.
   - Something else they prefer.

   Do not put the GitHub password, a recovery code or a TOTP secret in the repository, the portal or
   a public place. Send them only to the reviewer, by the channel they name.
5. **Check it yourself, as a stranger.** In a private window with no other sessions:
   1. sign in to <https://dayfile.app> with the reviewer's GitHub account and open **Log**;
   2. in claude.ai, add a custom connector with the URL `https://dayfile.cloud/mcp`;
   3. sign in, choose **read and write**, approve;
   4. ask Claude to list the open to-dos, add an event, close a to-do, then retract the event;
   5. open **Tokens** on dayfile.app and disconnect the connector. It should stop working at once.

## What a reviewer will find

| Tag | What | Tools it exercises |
|---|---|---|
| `backup` | seven nightly runs, one failure | list, get |
| `deploy` | five deploys, one rollback | list, get |
| `todo` | two open (one with a due date), two closed | list, add, close, retract |
| `heartbeat` | one open to-do, written by a silent-source check | list, get |

## When the review is over

The sample events are append-only, so to start clean delete the account on dayfile.app
(**Settings**, then delete). That erases the log. Make a new reviewer account for the next round.
