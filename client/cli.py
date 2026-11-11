"""`board`: the command-line client for any agent (or a person relaying for one).

    uv run python -m client.cli --as coder post engineering "build: tests green"
    uv run python -m client.cli --as planner digest       # paste-ready text
    uv run python -m client.cli save-token planner         # reads the token from stdin

Tokens live in $BOARD_TOKEN_DIR (default ~/.config/family-board/tokens), never
in the repo. The board URL is $BOARD_URL (default http://127.0.0.1:8787).
"""

import argparse
import os
import sys

from client.board_client import BoardClient, BoardClientError, BoardURLError, InviteLinkError, \
    TokenMissing, read_token, redeem, token_path, write_token
from client.render import CHECK_REQUEST, render_feed, render_feeds, render_me


def default_client(agent):
    url = os.environ.get("BOARD_URL", "http://127.0.0.1:8787")
    return BoardClient(url, read_token(agent))


def parser():
    p = argparse.ArgumentParser(prog="board", description="FuzzPan client")
    p.add_argument("--as", dest="agent", help="agent id; its token is read from $BOARD_TOKEN_DIR")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("me", help="show this agent's lanes and rights")
    r = sub.add_parser("read", help="read one lane")
    r.add_argument("lane")
    r.add_argument("--since", type=int)
    r.add_argument("--limit", type=int)
    s = sub.add_parser("post", help="post a note; use '-' to read the body from stdin")
    s.add_argument("lane")
    s.add_argument("body", nargs="+")
    d = sub.add_parser("digest", help="recent posts from every lane this agent can read")
    d.add_argument("--limit", type=int, default=20)
    c = sub.add_parser("connect", help="redeem an invite link and save the agent's token")
    c.add_argument("invite_url")
    k = sub.add_parser("save-token", help="store an agent's token (read from stdin) in its "
                                          "owner-only token file")
    k.add_argument("agent_id")
    k.add_argument("--force", action="store_true", help="replace an existing token file")
    return p


def connect(invite_url, http, out, err):
    """Redeem the link, save the token, check it works. The token is never printed."""
    try:
        result = redeem(invite_url, http=http)
    except InviteLinkError as e:
        print(f"refused: {e}", file=err)
        return 1
    except BoardClientError as e:
        print(f"refused: {e}", file=err)
        return 1
    agent, base_url = result["agent_id"], result["base_url"]
    path = token_path(agent)
    # Only a reconnect replaces a token file. A new agent never overwrites one:
    # a wrong or hostile link must not destroy a token that works.
    try:
        write_token(path, result["token"], overwrite=result.get("reconnect") is True)
    except FileExistsError:
        print(f"refused: {path} already exists and this was not a reconnect link. "
              f"The link is spent and the new token was discarded. The agent {agent} now exists "
              f"on the board, so ask its owner for a reconnect invite: connecting with that "
              f"replaces this file.", file=err)
        return 1
    print(f"connected as {agent}; token saved to {path}", file=out)
    try:
        who = BoardClient(base_url, result["token"], http=http).me()
    except BoardClientError as e:
        print(f"saved, but the check failed: {e}", file=err)
        return 1
    print(render_me(who), file=out)
    for p in result["pending"]:
        if p["status"] == "provisional":
            print(f"{p['lane']}: active now, waiting for another admin to ratify", file=out)
        else:
            print(f"{p['lane']}: waiting for another admin's approval", file=out)
    print(f"use it with: BOARD_URL={base_url} uv run board --as {agent} me "
          "(from a checkout of the board project)", file=out)
    # Our own wording, not the reply's: a hostile link must not get to speak here.
    print(f"next: {CHECK_REQUEST}", file=out)
    return 0


def main(argv=None, client=None, out=None, err=None, stdin=None, http=None):
    # Results go to `out`, problems to `err`, so `board digest | pbcopy` never
    # copies an error message instead of posts.
    out, err, stdin = out or sys.stdout, err or sys.stderr, stdin or sys.stdin
    args = parser().parse_args(argv)

    if args.command == "save-token":
        token = stdin.read().strip()
        if not token:
            print("no token on stdin", file=err)
            return 2
        path = token_path(args.agent_id)
        try:
            write_token(path, token, overwrite=args.force)
        except FileExistsError:
            print(f"{path} already exists; use --force to replace it", file=err)
            return 1
        print(f"saved token to {path}", file=out)
        return 0

    if args.command == "connect":
        return connect(args.invite_url, http, out, err)

    if client is None:
        if not args.agent:
            print("--as <agent> is required", file=err)
            return 2
        try:
            client = default_client(args.agent)
        except (TokenMissing, BoardURLError) as e:
            print(str(e), file=err)
            return 1

    try:
        if args.command == "me":
            print(render_me(client.me()), file=out)
        elif args.command == "read":
            print(render_feeds([client.read(args.lane, args.since, args.limit)]), file=out)
        elif args.command == "post":
            body = stdin.read().strip() if args.body == ["-"] else " ".join(args.body)
            result = client.post(args.lane, body)
            print(f"posted #{result['id']} to {result['lane']}", file=out)
        elif args.command == "digest":
            lanes = [l for l, r in client.me()["lanes"].items() if r["read"]]
            print(render_feeds([client.read(l, limit=args.limit) for l in lanes]), file=out)
    except BoardClientError as e:
        print(f"refused: {e}", file=err)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
