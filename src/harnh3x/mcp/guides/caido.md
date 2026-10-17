Browse and replay HTTP traffic in the local Caido proxy: history, replays, scope, and findings. To call a public JSON API with no proxy, use http.

## Conventions

- Search history with HTTPQL (`caido_list_requests`), then fetch one request by id. Replay with `caido_send_request` or `caido_edit_request` when the request should be sent again, so it shows up in the project.
- Check scope before testing a host. Record confirmed issues with the findings tools.
- Sensitive headers come back redacted unless the server was started with that opt-in. Do not invent cookie or token values the tool did not return.
- Narrow large results with `limit`, `bodyLimit`, or a tighter HTTPQL query instead of repeating a broad call.
