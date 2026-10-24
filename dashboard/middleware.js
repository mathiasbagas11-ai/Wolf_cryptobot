import { NextResponse } from "next/server";

// One password for the whole site. A Vercel URL is public, and this page shows
// the ledger and the open book, so it refuses outright rather than serving
// everyone when the password was never set.
export function middleware(req) {
  const password = process.env.DASHBOARD_PASSWORD;
  if (!password) {
    return new NextResponse("Set DASHBOARD_PASSWORD in Vercel environment variables.", { status: 503 });
  }
  const header = req.headers.get("authorization") || "";
  const [scheme, encoded] = header.split(" ");
  if (scheme === "Basic" && encoded) {
    const decoded = atob(encoded);
    const given = decoded.slice(decoded.indexOf(":") + 1);
    if (given === password) return NextResponse.next();
  }
  return new NextResponse("Authentication required", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="Wolf"' },
  });
}

export const config = { matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"] };
