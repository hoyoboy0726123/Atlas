import type { Metadata } from 'next'
import './globals.css'
import { Toaster } from 'sonner'

export const metadata: Metadata = {
  title: 'Atlas',
  description: 'Atlas — 視覺化工作流自動化系統',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-TW">
      <head>
        {/* Atlas Hero (Soft Architectural Grid) 字型載入。
            Bricolage Grotesque - 標題;Plus Jakarta Sans - body;JetBrains Mono - metadata。
            放在 RootLayout 內、整個 app 都可透過 font-family 使用、不需重複載入。 */}
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,400..700&family=JetBrains+Mono:wght@400;500&family=Plus+Jakarta+Sans:wght@400..700&display=swap"
          rel="stylesheet"
        />
      </head>
      <body>
        <div className="h-screen overflow-hidden bg-white flex flex-col">
          <main className="flex-1 overflow-hidden flex flex-col min-w-0">
            {children}
          </main>
        </div>
        <Toaster position="top-right" richColors />
      </body>
    </html>
  )
}
