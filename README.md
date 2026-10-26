# Agency Connect

Drayton's private MCP connector for Mission Control. Gives Claude real actions through his own accounts:
`send_sms` (Twilio, to prospects), `text_drayton` (Twilio, to the owner), `stripe_snapshot` (optional), `server_status`.

## Deploy on Render (Blueprint)
1. render.com → sign in with GitHub → **New +** → **Blueprint** → select this repo → **Apply**.
2. Render asks for: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` (Twilio Console → Account Info), `TWILIO_FROM` (your registered Twilio number, `+1918...`), `OWNER_PHONE` (your cell, `+1918...`). `MCP_TOKEN` is generated for you.
3. When the service is Live, open `https://<service>.onrender.com/health` → `{"ok":true,...}`.
4. Copy `MCP_TOKEN` from the service's **Environment** tab.

## Connect to Claude
claude.ai → Customize → Connectors → **+ Add** → **Add custom connector** → name `Agency Connect`, URL `https://<service>.onrender.com/mcp` → No sign in → request header `Authorization` = `Bearer <MCP_TOKEN>` → Add.

## Keep the free tier awake
Free Render services sleep after 15 idle minutes. Either upgrade the instance to Starter, or add a free UptimeRobot monitor that hits `/health` every 5 minutes.

## Env vars
`MCP_TOKEN` (required), `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM`, `OWNER_PHONE`, `STRIPE_SECRET_KEY` (optional, restricted read key), `SMS_PER_HOUR` (default 40).
