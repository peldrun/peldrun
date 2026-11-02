//frontend\src\app\projects\[projectId]\chat\[chatId]\page.tsx
import React from "react";

interface ProjectChatPageProps {
  params: Promise<{ projectId: string; chatId: string }>;
}

export default async function ProjectChatPage({ params }: ProjectChatPageProps) {
  const resolvedParams = await params;
  const { projectId, chatId } = resolvedParams;

  return (
    <div className="flex flex-col items-center justify-center h-screen bg-slate-950 text-slate-100 font-mono p-6">
      <div className="max-w-md w-full p-6 rounded-sm border border-slate-800 bg-slate-900 shadow-xl space-y-4">
        <h1 className="text-sm font-bold text-emerald-400 uppercase tracking-wider">Independent Project Workspace</h1>
        <div className="text-xs space-y-1 text-slate-300">
          <p><span className="text-slate-500">Project ID:</span> {projectId}</p>
          <p><span className="text-slate-500">Chat Session ID:</span> {chatId}</p>
        </div>
        <p className="text-[11px] text-slate-500 leading-relaxed">
          This isolated project context ensures clean URL routing and dedicated session repositories.
        </p>
      </div>
    </div>
  );
}
