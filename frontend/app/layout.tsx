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
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
