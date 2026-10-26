"""Made-up sessions for `tmux-ai-nav demo`: try the picker, or take screenshots, without
exposing real work."""
import time

from .agents import Agent

M, H, D = 60, 3600, 86400

# (tmux session, window name, title, status, age, flags, branch, you, claude)
SESSIONS = [
    ("1web", "checkout-flow", "Add Apple Pay to checkout", "waiting", 2 * M, "", "feat/apple-pay",
     "add apple pay as a payment option on the checkout page",
     "Apple Pay needs a merchant identifier and a domain verification file. I can scaffold both, "
     "but I need to know which payment processor you use. Stripe or Braintree?"),
    ("2api", "rate-limits", "Per-key rate limiting for the public API", "idle", 6 * M, "done", "rate-limit",
     "add per-key rate limiting to the public api, 100 req/min default",
     "Done. Rate limiting uses a **sliding window in Redis**, keyed by API key.\n\n"
     "| Setting | Default | Override |\n|---|---|---|\n"
     "| Requests per minute | 100 | per key |\n"
     "| Burst | 20 | global only |\n\n"
     "Added 12 tests; all pass. Responses now include `X-RateLimit-*` headers."),
    ("3infra", "", "Terraform plan for the staging VPC", "idle", 3 * H + 12 * M, "later", "main",
     "draft the terraform for a separate staging vpc",
     "The plan creates 23 resources: the VPC, 3 public and 3 private subnets, NAT gateways and routes. "
     "Nothing is applied yet. Review modules/vpc/main.tf before running apply."),
    ("1web", "dark-mode", "Dark mode for the settings pages", "busy", 4 * M, "", "dark-mode",
     "finish dark mode for the settings pages", "Updating the color tokens in settings/*.tsx..."),
    ("2api", "flaky-tests", "Fix flaky webhook retry tests", "busy", 1 * M, "", "fix/flaky-retry",
     "the webhook retry tests fail about 1 in 10 runs, find out why",
     "The retry scheduler reads the real clock. Switching the tests to a fake timer..."),
    ("4mobile", "push-notifs", "Push notification preferences", "idle", 25 * M, "", "notif-prefs",
     "let users choose which push notifications they get",
     "Added a preferences screen with per-category toggles, stored server side. "
     "Context is getting full; consider starting a fresh session for the iOS work."),
    ("1web", "seo-audit", "SEO audit of marketing pages", "idle", 48 * M, "", "main",
     "audit the marketing pages for seo problems",
     "Found **14 issues**:\n\n- 6 pages missing meta descriptions\n- 3 duplicate titles\n"
     "- images without alt text on the pricing page\n\nFixes are in a draft PR."),
    ("2api", "", "Upgrade to Postgres 16", "shell", 1 * H + 5 * M, "", "pg16",
     "upgrade the dev database to postgres 16", "Running the migration test suite in the background..."),
    ("3infra", "ci-cache", "Speed up CI with dependency caching", "idle", 2 * H + 30 * M, "pin", "ci-cache",
     "ci takes 14 minutes, can we cache dependencies?",
     "Cached node_modules and the Docker layers. CI now takes about 6 minutes on a warm cache."),
    ("1web", "a11y", "Keyboard navigation in the editor", "idle", 4 * H, "", "a11y-editor",
     "make the editor usable with just a keyboard",
     "Every toolbar action now has a shortcut, focus is trapped in dialogs, and there's a visible "
     "focus ring. Screen reader labels still need a pass."),
    ("4mobile", "", "Crash on Android 12 when rotating", "idle", 5 * H + 40 * M, "", "fix/rotate-crash",
     "the app crashes on android 12 when you rotate during upload",
     "The upload fragment held a reference to the destroyed activity. Moved the upload into a "
     "ViewModel; rotation no longer crashes."),
    ("2api", "openapi", "Generate OpenAPI docs from routes", "idle", 7 * H, "", "openapi",
     "generate openapi docs from our route definitions",
     "Docs are generated at build time and served at /docs. Three routes had no schemas; I added them."),
    ("3infra", "", "Rotate the database credentials", "idle", 9 * H, "", "main",
     "rotate the prod database credentials", "Rotated through the secrets manager. Old credentials are revoked."),
    ("1web", "i18n", "German translations for onboarding", "idle", 20 * H, "", "i18n-de",
     "add german translations for onboarding", "All 86 onboarding strings are translated."),
    ("2api", "", "Paginate the audit log endpoint", "idle", 2 * D, "", "audit-pagination",
     "the audit log endpoint times out for big accounts", "Switched to cursor pagination."),
    ("3infra", "logs", "Ship logs to the new collector", "idle", 3 * D, "", "logs",
     "send app logs to the new collector", "Done, with a 7 day retention."),
    ("4mobile", "", "App store screenshots", "idle", 4 * D, "", "main",
     "generate the app store screenshots", "Generated 5 screenshots per device size."),
    ("1web", "", "Remove the old feature flags", "idle", 6 * D, "", "cleanup-flags",
     "remove feature flags that are fully rolled out", "Removed 9 flags and their dead code paths."),
]


# what an extras command (see extras.py) might add: session id index -> (badge, style, preview lines)
EXTRAS = {
    0: ("✎3", "info", ["3 uncommitted files", "1 commit not pushed"]),
    1: ("", "dim", ["12 tests added, all passing"]),
    5: ("84%", "warn", ["context 84% used"]),
}


# scheduled loops (CronCreate, /loop) still running: session index -> loops
LOOPS = {
    7: [{"kind": "cron", "schedule": "Every 5 minutes", "prompt": "/check-migration", "next": None}],
}


class Demo:
    def __init__(self, now=None):
        self.t0 = now or time.time()
        self.later = set()
        self.pins = set()
        self.list = []
        for i, (sess, win, title, status, age, flags, branch, you, said) in enumerate(SESSIONS):
            project = sess.lstrip("0123456789")
            a = Agent(kind="claude", pid=1000 + i, session_id=f"demo-{i:02d}", name=project, status=status,
                      cwd=f"~/code/{project}", since=self.t0 - age, started=self.t0 - age - 2 * H,
                      pane_id=f"%{i + 1}", tmux_session_id=f"${sess[0]}", tmux_session=sess,
                      window_id=f"@{i + 1}", window_index=i % 9 + 1, window_name=win or "claude",
                      pane_title=f"✳ {title}", branch=branch, title=title)
            a.session_ids = [a.tmux_session_id]
            a.done = "done" in flags
            if "later" in flags:
                self.later.add(a.session_id)
            if "pin" in flags:
                self.pins.add(a.session_id)
            if i in EXTRAS:
                a.badge, a.badge_style, a.extra_lines = EXTRAS[i]
            a.loops = LOOPS.get(i, [])
            a.summary = {"user": you, "assistant": said}
            self.list.append(a)
        self.here = None
        self.opener = ("$1", "1web", "~/code/web")  # as if opened from a pane in the web session

    def agents(self):
        for a in self.list:
            a.later = a.session_id in self.later
            a.pinned = a.session_id in self.pins
        return list(self.list)

    def toggle_later(self, agent):
        if agent.session_id in self.later:
            self.later.discard(agent.session_id)
            return False
        self.later.add(agent.session_id)
        return True

    def toggle_pin(self, agent):
        if agent.session_id in self.pins:
            self.pins.discard(agent.session_id)
            return False
        self.pins.add(agent.session_id)
        return True

    def summary(self, session_id):
        return next((a.summary for a in self.list if a.session_id == session_id), {})
