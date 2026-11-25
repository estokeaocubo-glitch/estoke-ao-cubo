// Roteador por hash: #/login, #/primeiro-acesso, #/inicio, #/m/<slug> (sidebar), #/t/<slug> (topbar).

const routes = [];
let guard = null;
let fallback = "#/inicio";

export function route(pattern, handler) {
  const keys = [];
  const regex = new RegExp(
    "^" + pattern.replace(/:([a-z_]+)/gi, (_, k) => (keys.push(k), "([^/]+)")) + "$"
  );
  routes.push({ regex, keys, handler });
}

export function setGuard(fn) {
  guard = fn;
}

export function setFallback(hash) {
  fallback = hash;
}

export function current() {
  return (location.hash || "").replace(/^#/, "") || "/";
}

export function navigate(path, { replace = false } = {}) {
  const hash = "#" + path;
  if (location.hash === hash) {
    resolve();
    return;
  }
  if (replace) {
    history.replaceState(null, "", hash);
    resolve();
  } else {
    location.hash = hash;
  }
}

export function resolve() {
  const path = current();
  if (guard) {
    const redirect = guard(path);
    if (redirect && redirect !== path) {
      navigate(redirect, { replace: true });
      return;
    }
  }
  for (const r of routes) {
    const m = path.match(r.regex);
    if (m) {
      const params = {};
      r.keys.forEach((k, i) => (params[k] = decodeURIComponent(m[i + 1])));
      r.handler(params, path);
      return;
    }
  }
  navigate(fallback.replace(/^#/, ""), { replace: true });
}

export function start() {
  window.addEventListener("hashchange", resolve);
  resolve();
}
