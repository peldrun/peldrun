// frontend/src/components/generative-ui/ErrorBoundary.tsx
"use client";

import React, { Component, ErrorInfo, ReactNode } from "react";
import { MarkdownRenderer } from "@/components/chat/markdown-renderer";

interface Props {
  fallbackContent: string;
  children: ReactNode;
  onCatch?: (error: Error) => void;
}

interface State {
  hasError: boolean;
  errorMessage: string | null;
}

export class GenerativeUIErrorBoundary extends Component<Props, State> {
  public state: State = {
    hasError: false,
    errorMessage: null,
  };

  public static getDerivedStateFromError(error: Error): State {
    return { hasError: true, errorMessage: error.message };
  }

  public componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    if (this.props.onCatch) {
      this.props.onCatch(error);
    }
  }

  public render() {
    if (this.state.hasError) {
      return (
        <div className="space-y-2">
          <div className="text-[10px] text-amber-500/80 font-mono flex items-center gap-1.5 px-2 py-0.5 rounded bg-amber-500/10 border border-amber-500/20 w-fit">
            <span>Render fallback: Displaying as standard text</span>
          </div>
          <MarkdownRenderer content={this.props.fallbackContent} />
        </div>
      );
    }

    return this.props.children;
  }
}
