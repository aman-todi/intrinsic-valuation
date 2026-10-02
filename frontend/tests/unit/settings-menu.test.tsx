import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { SettingsMenu } from "@/components/settings-menu";
import { createMockServer } from "@/mocks/node";
import { THEME_STORAGE_KEY } from "@/lib/theme";

const signOut = vi.fn(async () => {});
vi.mock("@/components/auth-provider", () => ({
  useAuth: () => ({ user: { email: "a@example.com" }, devBypass: false, signOut }),
}));

const { server, state } = createMockServer();
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  state.reset();
  signOut.mockClear();
  window.localStorage.clear();
  delete document.documentElement.dataset.theme;
});
afterAll(() => server.close());

function renderMenu() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <SettingsMenu />
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

describe("SettingsMenu", () => {
  it("switches between light, dark and system appearance and remembers the choice", async () => {
    const user = renderMenu();
    await user.click(screen.getByRole("button", { name: "Settings" }));
    expect(screen.getByText("a@example.com")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "System" })).toHaveAttribute("aria-checked", "true");

    await user.click(screen.getByRole("radio", { name: "Dark" }));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");

    await user.click(screen.getByRole("radio", { name: "Light" }));
    expect(document.documentElement.dataset.theme).toBe("light");

    await user.click(screen.getByRole("radio", { name: "System" }));
    expect(document.documentElement.dataset.theme).toBeUndefined();
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBeNull();
  });

  it("deletes the account only after typing DELETE, then signs out", async () => {
    const user = renderMenu();
    await user.click(screen.getByRole("button", { name: "Settings" }));
    await user.click(screen.getByRole("button", { name: "Delete account…" }));

    const confirm = screen.getByRole("button", { name: "Delete account" });
    expect(confirm).toBeDisabled();
    await user.type(screen.getByLabelText(/to confirm/), "delete");
    expect(confirm).toBeDisabled(); // exact, case-sensitive word
    await user.clear(screen.getByLabelText(/to confirm/));
    await user.type(screen.getByLabelText(/to confirm/), "DELETE");
    await user.click(confirm);

    await waitFor(() => expect(signOut).toHaveBeenCalledTimes(1));
  });

  it("shows the server's reason when deletion is refused", async () => {
    server.use(
      http.delete("*/api/me", () =>
        HttpResponse.json({ detail: "A valuation is still being built." }, { status: 409 }),
      ),
    );
    const user = renderMenu();
    await user.click(screen.getByRole("button", { name: "Settings" }));
    await user.click(screen.getByRole("button", { name: "Delete account…" }));
    await user.type(screen.getByLabelText(/to confirm/), "DELETE");
    await user.click(screen.getByRole("button", { name: "Delete account" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("A valuation is still being built.");
    expect(signOut).not.toHaveBeenCalled();
  });
});
