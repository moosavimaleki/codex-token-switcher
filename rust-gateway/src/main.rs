use axum::{
    body::{to_bytes, Body},
    extract::State,
    http::{header, Request, StatusCode},
    response::Response,
    routing::{any, get},
    Router,
};
use reqwest::Client;
use serde_json::Value;
use std::{
    cmp::Ordering,
    collections::HashMap,
    env, fs,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::sync::RwLock;

#[derive(Clone)]
struct AppState {
    accounts_dir: PathBuf,
    status_dir: PathBuf,
    models_cache: PathBuf,
    upstream_base: String,
    api_key: String,
    client: Client,
    cursor: Arc<Mutex<usize>>,
    bindings: Arc<RwLock<HashMap<String, String>>>,
}

#[derive(Clone)]
struct Candidate {
    name: String,
    access_token: String,
    account_id: Option<String>,
    score: f64,
}

fn json(path: &Path) -> Option<Value> {
    serde_json::from_slice(&fs::read(path).ok()?).ok()
}

fn text(value: Option<&Value>) -> Option<String> {
    value
        .and_then(Value::as_str)
        .map(str::to_owned)
        .filter(|v| !v.is_empty())
}

fn account_candidates(state: &AppState) -> Vec<Candidate> {
    let Ok(entries) = fs::read_dir(&state.accounts_dir) else {
        return Vec::new();
    };
    let mut out = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|v| v.to_str()) != Some("json") {
            continue;
        }
        let Some(auth) = json(&path) else { continue };
        let Some(tokens) = auth.get("tokens").and_then(Value::as_object) else {
            continue;
        };
        let Some(access_token) = text(tokens.get("access_token")) else {
            continue;
        };
        let name = path
            .file_stem()
            .and_then(|v| v.to_str())
            .unwrap_or_default()
            .to_owned();
        let status = json(&state.status_dir.join(format!("{name}.json"))).unwrap_or(Value::Null);
        if matches!(
            status.get("state").and_then(Value::as_str),
            Some("needs_login" | "error")
        ) {
            continue;
        }
        let snapshots = status
            .get("rate_limits")
            .and_then(|v| v.get("snapshots"))
            .and_then(Value::as_array);
        let codex = snapshots.and_then(|items| {
            items
                .iter()
                .find(|v| v.get("limit_id").and_then(Value::as_str) == Some("codex"))
                .or_else(|| items.first())
        });
        let primary = codex
            .and_then(|v| v.get("primary"))
            .and_then(|v| v.get("remaining_percent"))
            .and_then(Value::as_f64)
            .unwrap_or(0.0);
        let secondary = codex
            .and_then(|v| v.get("secondary"))
            .and_then(|v| v.get("remaining_percent"))
            .and_then(Value::as_f64)
            .unwrap_or(0.0);
        let available = [primary, secondary]
            .into_iter()
            .filter(|value| *value > 0.0)
            .collect::<Vec<_>>();
        if available.is_empty() {
            continue;
        }
        let score = available.iter().copied().fold(f64::INFINITY, f64::min) * 1000.0
            + available.iter().sum::<f64>();
        let account_id = text(tokens.get("account_id")).or_else(|| {
            auth.get("account_id")
                .and_then(Value::as_str)
                .map(str::to_owned)
        });
        out.push(Candidate {
            name,
            access_token,
            account_id,
            score,
        });
    }
    out.sort_by(|a, b| {
        b.score
            .partial_cmp(&a.score)
            .unwrap_or(Ordering::Equal)
            .then_with(|| a.name.cmp(&b.name))
    });
    out
}

fn local_key(request: &Request<Body>) -> Option<String> {
    request
        .headers()
        .get("x-conversation-id")
        .and_then(|v| v.to_str().ok())
        .map(str::to_owned)
        .or_else(|| {
            request
                .headers()
                .get("x-thread-id")
                .and_then(|v| v.to_str().ok())
                .map(str::to_owned)
        })
}

async fn health() -> &'static str {
    "ok\n"
}

async fn models(State(state): State<AppState>, request: Request<Body>) -> Response<Body> {
    if !authorized(&state, &request) {
        return response(StatusCode::UNAUTHORIZED, "invalid local API key\n");
    }
    let body = serde_json::json!({
        "object": "list",
        "data": available_models(&state)
    });
    Response::builder()
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, "application/json")
        .body(Body::from(body.to_string()))
        .unwrap()
}

fn available_models(state: &AppState) -> Vec<Value> {
    let models = json(&state.models_cache)
        .and_then(|cache| cache.get("models").and_then(Value::as_array).cloned())
        .unwrap_or_default()
        .into_iter()
        .filter(|model| model.get("supported_in_api").and_then(Value::as_bool) == Some(true))
        .filter_map(|model| {
            let id = model.get("slug").and_then(Value::as_str)?;
            Some(serde_json::json!({
                "id": id,
                "object": "model",
                "owned_by": "codex"
            }))
        })
        .collect::<Vec<_>>();
    if models.is_empty() {
        vec![
            serde_json::json!({"id": "gpt-5.6-sol", "object": "model", "owned_by": "codex"}),
            serde_json::json!({"id": "gpt-5.6-terra", "object": "model", "owned_by": "codex"}),
            serde_json::json!({"id": "gpt-5.6-luna", "object": "model", "owned_by": "codex"}),
        ]
    } else {
        models
    }
}

fn normalize_codex_request(body: &[u8]) -> Result<(Vec<u8>, bool), String> {
    let mut value = serde_json::from_slice::<Value>(body)
        .map_err(|error| format!("invalid JSON request body: {error}"))?;
    let object = value
        .as_object_mut()
        .ok_or_else(|| "request body must be a JSON object".to_string())?;
    let client_streams = object
        .get("stream")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if let Some(input) = object.get_mut("input") {
        match input {
            Value::String(text) => {
                *input = serde_json::json!([{
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                }]);
            }
            Value::Object(_) => *input = Value::Array(vec![input.clone()]),
            _ => {}
        }
    }
    // The Codex ChatGPT backend always streams and does not support stored Responses.
    object.insert("stream".to_string(), Value::Bool(true));
    object.insert("store".to_string(), Value::Bool(false));
    serde_json::to_vec(&value)
        .map(|body| (body, client_streams))
        .map_err(|error| format!("could not serialize request body: {error}"))
}

fn authorized(state: &AppState, request: &Request<Body>) -> bool {
    request
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .is_some_and(|v| v == state.api_key)
}

async fn gateway(State(state): State<AppState>, request: Request<Body>) -> Response<Body> {
    if !authorized(&state, &request) {
        return response(StatusCode::UNAUTHORIZED, "invalid local API key\n");
    }
    if request.uri().path() != "/v1/responses" {
        return response(StatusCode::NOT_FOUND, "only /v1/responses is supported\n");
    }
    let conversation = local_key(&request);
    let candidates = account_candidates(&state);
    if candidates.is_empty() {
        return response(
            StatusCode::SERVICE_UNAVAILABLE,
            "no account has available cached Codex quota\n",
        );
    }
    let selected = if let Some(key) = conversation.as_deref() {
        let bindings = state.bindings.read().await;
        candidates
            .iter()
            .find(|candidate| bindings.get(key) == Some(&candidate.name))
            .cloned()
    } else {
        None
    };
    let start = {
        let mut cursor = state.cursor.lock().expect("cursor lock");
        let start = *cursor % candidates.len();
        *cursor = (*cursor + 1) % candidates.len();
        start
    };
    let mut candidates_to_try = Vec::with_capacity(candidates.len());
    if let Some(candidate) = selected {
        candidates_to_try.push(candidate);
    }
    for offset in 0..candidates.len() {
        let candidate = candidates[(start + offset) % candidates.len()].clone();
        if !candidates_to_try
            .iter()
            .any(|selected| selected.name == candidate.name)
        {
            candidates_to_try.push(candidate);
        }
    }
    let (parts, body) = request.into_parts();
    let body = match to_bytes(body, 20 * 1024 * 1024).await {
        Ok(value) => value,
        Err(_) => return response(StatusCode::PAYLOAD_TOO_LARGE, "request body too large\n"),
    };
    let (upstream_body, client_streams) = match normalize_codex_request(&body) {
        Ok(value) => value,
        Err(error) => return response(StatusCode::BAD_REQUEST, format!("{error}\n")),
    };
    let target = format!("{}/responses", state.upstream_base.trim_end_matches('/'));
    let upstream = 'accounts: loop {
        for candidate in &candidates_to_try {
            let mut builder = state.client.post(&target).body(upstream_body.clone());
            for (name, value) in &parts.headers {
                if *name != header::AUTHORIZATION
                    && *name != header::HOST
                    && *name != header::CONTENT_LENGTH
                {
                    builder = builder.header(name, value);
                }
            }
            builder = builder
                .header(
                    header::AUTHORIZATION,
                    format!("Bearer {}", candidate.access_token),
                )
                .header(header::USER_AGENT, "codex-cli");
            if let Some(account_id) = &candidate.account_id {
                builder = builder.header("ChatGPT-Account-ID", account_id);
            }
            let upstream = match builder.send().await {
                Ok(value) => value,
                Err(error) => {
                    return response(
                        StatusCode::BAD_GATEWAY,
                        format!("upstream request failed: {error}\n"),
                    )
                }
            };
            if matches!(upstream.status().as_u16(), 401 | 403 | 429) {
                continue;
            }
            if let Some(key) = &conversation {
                state
                    .bindings
                    .write()
                    .await
                    .insert(key.clone(), candidate.name.clone());
            }
            break 'accounts upstream;
        }
        return response(
            StatusCode::TOO_MANY_REQUESTS,
            "all candidate accounts rejected the request or have exhausted quota\n",
        );
    };
    let status =
        StatusCode::from_u16(upstream.status().as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
    if !client_streams && status.is_success() {
        let bytes = match upstream.bytes().await {
            Ok(value) => value,
            Err(error) => {
                return response(
                    StatusCode::BAD_GATEWAY,
                    format!("upstream stream failed: {error}\n"),
                )
            }
        };
        if let Some(completed) = completed_response(&bytes) {
            return json_response(StatusCode::OK, &completed);
        }
        if let Ok(value) = serde_json::from_slice::<Value>(&bytes) {
            return json_response(StatusCode::OK, &value);
        }
        return response(
            StatusCode::BAD_GATEWAY,
            "upstream stream had no completed response\n",
        );
    }
    let mut output = Response::builder().status(status);
    for (name, value) in upstream.headers() {
        if *name != header::CONTENT_LENGTH {
            output = output.header(name, value);
        }
    }
    output
        .body(Body::from_stream(upstream.bytes_stream()))
        .unwrap_or_else(|_| {
            response(
                StatusCode::BAD_GATEWAY,
                "could not build upstream response\n",
            )
        })
}

fn completed_response(body: &[u8]) -> Option<Value> {
    let mut output_items = Vec::new();
    let mut completed = None;
    for line in String::from_utf8_lossy(body).lines() {
        let Some(data) = line.strip_prefix("data:").map(str::trim) else {
            continue;
        };
        let Ok(event) = serde_json::from_str::<Value>(data) else {
            continue;
        };
        match event.get("type").and_then(Value::as_str) {
            Some("response.output_item.done") => {
                if let Some(item) = event.get("item") {
                    output_items.push(item.clone());
                }
            }
            Some("response.completed") => {
                completed = event.get("response").cloned();
            }
            _ => {}
        }
    }
    let mut response = completed?;
    if !output_items.is_empty()
        && response
            .get("output")
            .and_then(Value::as_array)
            .is_none_or(Vec::is_empty)
    {
        response["output"] = Value::Array(output_items);
    }
    Some(response)
}

fn json_response(status: StatusCode, value: &Value) -> Response<Body> {
    Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, "application/json")
        .body(Body::from(value.to_string()))
        .unwrap()
}

fn response(status: StatusCode, body: impl Into<Body>) -> Response<Body> {
    Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
        .body(body.into())
        .unwrap()
}

fn env_or(name: &str, default: String) -> String {
    env::var(name)
        .ok()
        .filter(|v| !v.trim().is_empty())
        .unwrap_or(default)
}

#[tokio::main]
async fn main() {
    let listen = env_or("CODEX_MANAGER_GATEWAY_LISTEN", "127.0.0.1:8787".into());
    let api_key = env_or("CODEX_MANAGER_GATEWAY_API_KEY", "change-me".into());
    let manager_home = PathBuf::from(env_or(
        "CODEX_MANAGER_HOME",
        format!("{}/.codex-manager", env::var("HOME").unwrap_or_default()),
    ));
    let proxy = env::var("CODEX_MANAGER_PROXY")
        .ok()
        .filter(|v| !v.trim().is_empty());
    let mut client = Client::builder().timeout(Duration::from_secs(900));
    if let Some(proxy) = proxy {
        client = client.proxy(reqwest::Proxy::all(proxy).expect("invalid proxy"));
    }
    let state = AppState {
        accounts_dir: manager_home.join("accounts"),
        status_dir: manager_home.join("status"),
        models_cache: PathBuf::from(env_or(
            "CODEX_MANAGER_MODELS_CACHE",
            format!(
                "{}/.codex/models_cache.json",
                env::var("HOME").unwrap_or_default()
            ),
        )),
        upstream_base: env_or(
            "CODEX_MANAGER_GATEWAY_UPSTREAM",
            "https://chatgpt.com/backend-api/codex".into(),
        ),
        api_key,
        client: client.build().expect("could not build HTTP client"),
        cursor: Arc::new(Mutex::new(0)),
        bindings: Arc::new(RwLock::new(HashMap::new())),
    };
    let app = Router::new()
        .route("/health", get(health))
        .route("/v1/models", get(models))
        .fallback(any(gateway))
        .with_state(state);
    let listener = tokio::net::TcpListener::bind(listen)
        .await
        .expect("could not bind gateway");
    axum::serve(listener, app).await.expect("gateway stopped");
}

#[cfg(test)]
mod tests {
    use super::{completed_response, normalize_codex_request};
    use serde_json::Value;

    #[test]
    fn extracts_completed_response_from_sse() {
        let body = br#"event: response.output_item.done
data: {"type":"response.output_item.done","item":{"type":"message","content":[{"type":"output_text","text":"hello"}]}}

event: response.completed
data: {"type":"response.completed","response":{"id":"resp_1","status":"completed","output":[]}}

"#;
        let completed = completed_response(body).expect("completed event");
        assert_eq!(completed.get("id").and_then(|v| v.as_str()), Some("resp_1"));
        assert_eq!(completed["output"][0]["content"][0]["text"], "hello");
    }

    #[test]
    fn ignores_incomplete_sse() {
        assert!(
            completed_response(b"data: {\"type\":\"response.output_text.delta\"}\n\n").is_none()
        );
    }

    #[test]
    fn normalizes_string_input_for_codex() {
        let (body, client_streams) =
            normalize_codex_request(br#"{"model":"gpt-5.6-terra","input":"hello"}"#)
                .expect("normalized request");
        let value: Value = serde_json::from_slice(&body).expect("JSON body");
        assert!(!client_streams);
        assert_eq!(value["input"][0]["type"], "message");
        assert_eq!(value["input"][0]["content"][0]["text"], "hello");
        assert_eq!(value["stream"], true);
        assert_eq!(value["store"], false);
    }

    #[test]
    fn preserves_list_input_and_streaming_mode() {
        let (body, client_streams) = normalize_codex_request(
            br#"{"model":"gpt-5.6-terra","input":[{"type":"message"}],"stream":true}"#,
        )
        .expect("normalized request");
        let value: Value = serde_json::from_slice(&body).expect("JSON body");
        assert!(client_streams);
        assert_eq!(value["input"][0]["type"], "message");
        assert_eq!(value["stream"], true);
    }
}
