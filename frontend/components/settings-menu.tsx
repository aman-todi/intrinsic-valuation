"use client";

/**
 * The header's settings menu (three-line button): signed-in email, appearance (system / light / dark),
 * account deletion (typed confirmation, DELETE /api/me) and sign-out.
 */
import { useQueryClient } from "@tanstack/react-query";
import * as React from "react";
import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError, api } from "@/lib/api-client";
import { THEME_OPTIONS, type ThemePreference, applyThemePreference, readThemePreference } from "@/lib/theme";
import { cn } from "@/lib/utils";

const CONFIRM_WORD = "DELETE";

function MenuIcon() {
  return (
    <svg aria-hidden viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round">
      <path d="M4 7h16M4 12h16M4 17h16" />
    </svg>
  );
}

export function SettingsMenu() {
  const { user, devBypass, signOut } = useAuth();
  const [open, setOpen] = React.useState(false);
  const [confirming, setConfirming] = React.useState(false);
  // The panel only renders after a click (never during SSR), so reading storage here is hydration-safe.
  const [theme, setTheme] = React.useState<ThemePreference>(() =>
    typeof window === "undefined" ? "system" : readThemePreference(),
  );
  const panelId = React.useId();
  const rootRef = React.useRef<HTMLDivElement>(null);
  const buttonRef = React.useRef<HTMLButtonElement>(null);

  React.useEffect(() => {
    if (!open) return;
    const onPointer = (e: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        buttonRef.current?.focus();
      }
    };
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const chooseTheme = (pref: ThemePreference) => {
    setTheme(pref);
    applyThemePreference(pref);
  };

  return (
    <div ref={rootRef} className="relative">
      <Button
        ref={buttonRef}
        variant="ghost"
        size="sm"
        aria-label="Settings"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((o) => !o)}
        className="px-2"
      >
        <MenuIcon />
      </Button>

      {open && (
        <div
          id={panelId}
          role="region"
          aria-label="Settings"
          className="absolute right-0 z-40 mt-2 w-72 rounded-lg border border-border bg-card p-3 text-sm text-card-foreground shadow-lg"
        >
          {user && (
            <div className="border-b border-border pb-3">
              <div className="text-xs text-muted-foreground">Signed in as</div>
              <div className="truncate font-medium">{user.email ?? "unknown"}</div>
            </div>
          )}

          <fieldset className="border-b border-border py-3">
            <legend className="mb-2 text-xs text-muted-foreground">Appearance</legend>
            <div role="radiogroup" aria-label="Appearance" className="grid grid-cols-3 gap-1 rounded-md bg-muted p-1">
              {THEME_OPTIONS.map((o) => (
                <button
                  key={o.value}
                  type="button"
                  role="radio"
                  aria-checked={theme === o.value}
                  onClick={() => chooseTheme(o.value)}
                  className={cn(
                    "rounded px-2 py-1.5 text-xs font-medium transition-colors",
                    theme === o.value ? "bg-card text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  {o.label}
                </button>
              ))}
            </div>
          </fieldset>

          <div className="flex flex-col gap-1 pt-3">
            {user && !devBypass && (
              <Button variant="ghost" size="sm" className="justify-start" onClick={() => void signOut()}>
                Sign out
              </Button>
            )}
            {user && (
              <Button
                variant="ghost"
                size="sm"
                className="justify-start text-destructive hover:bg-destructive/10"
                onClick={() => {
                  setOpen(false);
                  setConfirming(true);
                }}
              >
                Delete account…
              </Button>
            )}
          </div>
        </div>
      )}

      {confirming && (
        <DeleteAccountDialog
          devBypass={devBypass}
          onClose={() => {
            setConfirming(false);
            buttonRef.current?.focus();
          }}
          onDeleted={async () => {
            if (devBypass) window.location.replace(window.location.origin); // dev user: start over
            else await signOut();
          }}
        />
      )}
    </div>
  );
}

function DeleteAccountDialog({
  devBypass,
  onClose,
  onDeleted,
}: {
  devBypass: boolean;
  onClose: () => void;
  onDeleted: () => Promise<void>;
}) {
  const queryClient = useQueryClient();
  const [typed, setTyped] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const titleId = React.useId();
  const inputId = React.useId();

  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !busy && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (typed !== CONFIRM_WORD || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.deleteAccount();
      queryClient.clear();
      await onDeleted();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong. Please try again.");
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-[var(--overlay)] p-4">
      <form
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onSubmit={submit}
        className="w-full max-w-md rounded-lg border border-border bg-card p-6 text-card-foreground shadow-xl"
      >
        <h2 id={titleId} className="text-lg font-semibold">
          Delete your account?
        </h2>
        <p className="mt-2 text-sm text-muted-foreground">
          This permanently deletes your account, your valuation history and your downloads
          {devBypass ? "" : ", and removes your sign-in"}. It can&apos;t be undone.
        </p>
        <label htmlFor={inputId} className="mt-4 block text-sm">
          Type <span className="font-mono font-semibold">{CONFIRM_WORD}</span> to confirm
        </label>
        <Input
          id={inputId}
          autoFocus
          autoComplete="off"
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          className="mt-1"
          disabled={busy}
        />
        {error && (
          <p role="alert" className="mt-3 text-sm text-destructive">
            {error}
          </p>
        )}
        <div className="mt-6 flex justify-end gap-2">
          <Button variant="outline" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" variant="destructive" disabled={typed !== CONFIRM_WORD || busy}>
            {busy ? "Deleting…" : "Delete account"}
          </Button>
        </div>
      </form>
    </div>
  );
}
