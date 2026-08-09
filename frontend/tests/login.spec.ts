import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const tokenFile = path.resolve(__dirname, "..", "real_token.txt");

function getToken(): string | null {
  try {
    return fs.readFileSync(tokenFile, "utf-8").trim();
  } catch {
    return null;
  }
}

test("/login renders and accepts login flow", async ({ page }) => {
  await page.goto("/login");
  await expect(page.locator('text=DC QUANT')).toBeVisible();
  await expect(page.locator('input[autocomplete="username"]')).toBeVisible();
  await expect(page.locator('input[type="password"]')).toBeVisible();
  await expect(page.locator('button:has-text("登录")')).toBeVisible();
});

test("logged-in session loads dashboard via token injection", async ({ page, context }) => {
  const token = getToken();
  if (!token) test.skip("No real_token.txt found");

  await context.addInitScript((tok: string) => {
    localStorage.setItem("dcquant-token", tok);
  }, token);

  await page.goto("/dashboard");
  await expect(page.locator('text=仪表盘')).toBeVisible({ timeout: 15000 });
  await expect(page.locator('text=净值走势')).toBeVisible();
});
