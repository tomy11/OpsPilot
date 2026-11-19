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

  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      signal,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    // fetch only rejects when the request never got an answer (offline, server down).
    throw new ApiError(0, "เชื่อมต่อเซิร์ฟเวอร์ไม่ได้ ตรวจการเชื่อมต่อแล้วลองอีกครั้ง");
  }
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
