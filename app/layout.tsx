import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ADVoost 검색 분석 클론",
  description: "ADVoost 검색 SEO 진단 및 내부 분석 플랫폼 프로토타입",
  icons: {
    icon: "/icon.svg",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="ko">
      <body>{children}</body>
    </html>
  );
}
