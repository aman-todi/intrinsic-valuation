import type { Metadata } from "next";
import { Providers } from "@/components/providers";
import "./globals.css";

export const metadata: Metadata = {
  title: "Intrinsic Value",
  description: "Pick the right valuation model for a US-listed company and build it.",
};

/**
 * Root layout: QueryClientProvider, auth gate, ActiveRunProvider/Guard context and the app
 * shell (which goes `inert` while a model is building) all live in <Providers>.
 */
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="flex min-h-full flex-col">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
