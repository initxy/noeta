async function getJSON(url) {
  const resp = await fetch(url);
  let parsed = null;
  try {
    parsed = await resp.json();
  } catch (e) {
    parsed = null;
  }
  return { status: resp.status, body: parsed };
}

async function postJSON(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  let parsed = null;
  try {
    parsed = await resp.json();
  } catch (e) {
    parsed = null;
  }
  return { status: resp.status, body: parsed };
}

export { getJSON, postJSON };
