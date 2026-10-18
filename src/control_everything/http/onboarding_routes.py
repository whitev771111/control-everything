"""Onboarding transport handlers; gateway supplies authorization and error mapping."""

from urllib.parse import parse_qs

from control_everything.input_validation import ProfileError

GET_ROUTES = {"/api/workspace/onboarding", "/api/workspace/source-locations", "/api/workspace/project-candidates"}
POST_ROUTES = {"/api/workspace/sources", "/api/workspace/project-selection"}


def handle_get(handler, path):
    selection = handler.server.workspace.selection
    if path.path == "/api/workspace/onboarding":
        if path.query:
            raise ProfileError("invalid_query")
        value = selection.overview()
    else:
        query = parse_qs(path.query, keep_blank_values=True, strict_parsing=True)
        allowed = {"provider"} if path.path.endswith("source-locations") else {"provider", "offset", "limit"}
        if set(query) - allowed or "provider" not in query or any(len(values) != 1 for values in query.values()):
            raise ProfileError("invalid_query")
        if path.path.endswith("source-locations"):
            value = selection.locations(query["provider"][0])
        else:
            try:
                offset, limit = int(query.get("offset", ["0"])[0]), int(query.get("limit", ["50"])[0])
            except ValueError:
                raise ProfileError("invalid_query") from None
            value = selection.candidates(query["provider"][0], offset=offset, limit=limit)
    handler.reply(200, value)


def handle_post(handler, route, body):
    if route == "/api/workspace/sources":
        value = handler.profile().save_source(body)
        handler.server.workspace.selection.invalidate()
        return value
    return handler.server.workspace.selection.change(body)
