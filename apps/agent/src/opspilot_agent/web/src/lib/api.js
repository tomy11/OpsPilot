export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export async function request(
  path,
  { token, body, signal, method = "GET" } = {},
) {
  const headers = { Accept: "application/json" };
  if (token) headers.Authorization = "Bearer " + token;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  const response = await fetch(path, {
    method,
    headers,
    signal,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const message =
      typeof data?.detail === "string" ? data.detail : "บริการไม่พร้อม กรุณาลองใหม่";
    throw new ApiError(response.status, message);
  }
  return data;
}

export function formatTime(value) {
  const utc = /(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : value + "Z";
  return new Intl.DateTimeFormat("th-TH", {
    dateStyle: "short",
    timeStyle: "medium",
  }).format(new Date(utc));
}
