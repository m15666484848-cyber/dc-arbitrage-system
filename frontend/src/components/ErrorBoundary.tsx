import { Component, ReactNode } from "react";

interface State {
  hasError: boolean;
  error: Error | null;
  info: any;
}

export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { hasError: false, error: null, info: null };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error, info: null };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error("ErrorBoundary caught:", error, info);
    this.setState({ info });
  }

  render() {
    if (this.state.hasError) {
      const err = this.state.error;
      return (
        <div className="min-h-screen flex items-center justify-center p-4">
          <div className="glass p-6 max-w-2xl w-full">
            <h2 className="text-lg font-bold text-red-400 mb-3">应用发生错误</h2>
            <div className="text-sm text-red-300 mb-2">
              <strong>Name:</strong> {err?.name || "Unknown"}
            </div>
            <div className="text-sm text-red-300 mb-2">
              <strong>Message:</strong> {err?.message || "Unknown error"}
            </div>
            <pre className="text-xs text-slate-400 overflow-auto max-h-40 mb-4 whitespace-pre-wrap">
              {err?.stack || ""}
            </pre>
            {this.state.info?.componentStack && (
              <div className="mb-4">
                <strong className="text-sm text-amber-400">Component Stack:</strong>
                <pre className="text-xs text-slate-500 overflow-auto max-h-40 whitespace-pre-wrap">
                  {this.state.info.componentStack}
                </pre>
              </div>
            )}
            <button
              className="btn btn-primary"
              onClick={() => { this.setState({ hasError: false, error: null, info: null }); window.location.reload(); }}
            >
              刷新页面
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

