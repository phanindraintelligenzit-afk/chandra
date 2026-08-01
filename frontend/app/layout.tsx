import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";
import Providers from "@/app/providers";

export const metadata: Metadata = {
  title: "Chandra | L3 Digital Cloud Engineer",
  description:
    "A clean enterprise onboarding experience for a premium AI workforce dashboard."
};

export default function RootLayout({
  children
}: Readonly<{
  children: ReactNode;
}>) {
  return (
    <html lang="en" data-scroll-behavior="smooth">
      <body>
        <Providers>
          <div className="flex min-h-screen bg-gray-950">
                      <main className="flex-1 min-w-0">
              {children}
            </main>
          </div>
        </Providers>
      </body>
    </html>
  );
}