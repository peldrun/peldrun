// frontend/src/features/source-control/components/ProviderConfigDialog.tsx


import React, { useState, useEffect } from "react";
import { ProviderDescriptorResponse } from "../api/schemas";
import { getDynamicRedirectUri } from "../api/source-control-client";

interface ProviderConfigDialogProps {
  isOpen: boolean;
  provider: ProviderDescriptorResponse | null;
  isSaving: boolean;
  onClose: () => void;
  onSave: (clientId: string, clientSecret: string) => Promise<boolean>;
  onDelete: () => Promise<boolean>;
  existingClientId?: string;
}

/**
 * A dialog that allows users to input their own OAuth App credentials (Client ID & Secret).
 * Essential for local-first deployments where a central OAuth broker is not available.
 */
export function ProviderConfigDialog({
  isOpen,
  provider,
  isSaving,
  onClose,
  onSave,
  onDelete,
  existingClientId,
}: ProviderConfigDialogProps) {
  const [clientId, setClientId] = useState(existingClientId || "");
  const [clientSecret, setClientSecret] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);

  useEffect(() => {
    if (isOpen) {
      setClientId(existingClientId || "");
      setClientSecret("");
      setError(null);
      setShowDeleteConfirm(false);
    }
  }, [isOpen, existingClientId]);

  if (!isOpen || !provider) return null;

  // Dynamic URLs for creating OAuth apps based on the provider
  const getDocsUrl = () => {
    if (provider.provider_id === "github") {
      return "https://github.com/settings/developers";
    }
    if (provider.provider_id === "gitlab") {
      return "https://gitlab.com/-/profile/applications";
    }
    return "#";
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    if (!clientId.trim() || !clientSecret.trim()) {
      setError("Both Client ID and Client Secret are required.");
      return;
    }

    const success = await onSave(clientId.trim(), clientSecret.trim());
    if (success) {
      // Clear form on success
      setClientId("");
      setClientSecret("");
      onClose();
    } else {
      setError("Failed to save credentials. Please try again.");
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center overflow-y-auto bg-gray-900 bg-opacity-50 p-4">
      <div className="relative w-full max-w-lg rounded-lg bg-white shadow-xl">

        {/* Header */}
        <div className="border-b border-gray-200 px-6 py-4">
          <h2 className="text-xl font-semibold text-gray-900">
            Configure {provider.display_name} OAuth App
          </h2>
          <p className="mt-1 text-sm text-gray-500">
            PELDRUN runs locally. To connect securely, you must provide your own OAuth Application credentials.
          </p>
        </div>

        {/* Instructions */}
        <div className="bg-blue-50 px-6 py-3 border-b border-blue-100">
          <h3 className="text-sm font-medium text-blue-800 mb-1">How to get these keys:</h3>
          <ol className="list-decimal list-inside text-xs text-blue-700 space-y-1">
            <li>Go to your <a href={getDocsUrl()} target="_blank" rel="noopener noreferrer" className="font-semibold underline hover:text-blue-900">provider's developer settings</a>.</li>
            <li>Create a new "OAuth App" (or "Developer App").</li>
            <li>Set the <strong>Homepage URL</strong> to: <code className="bg-blue-100 px-1 rounded">{typeof window !== 'undefined' ? window.location.origin : ''}</code></li>
            <li>Set the <strong>Authorization callback URL</strong> to: <code className="bg-blue-100 px-1 rounded select-all">{getDynamicRedirectUri(provider.provider_id)}</code></li>
            <li>Copy the generated Client ID and Client Secret below.</li>
          </ol>
        </div>

        {/* Form */}
        <form onSubmit={handleSubmit} className="px-6 py-4">
          <div className="space-y-4">
            <div>
              <label htmlFor="clientId" className="block text-sm font-medium text-gray-700">
                Client ID
              </label>
              <input
                type="text"
                id="clientId"
                value={clientId}
                onChange={(e) => setClientId(e.target.value)}
                className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm shadow-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                placeholder="e.g., Ov23li..."
                disabled={isSaving}
              />
            </div>

            <div>
              <label htmlFor="clientSecret" className="block text-sm font-medium text-gray-700">
                Client Secret
              </label>
              <input
                type="password"
                id="clientSecret"
                value={clientSecret}
                onChange={(e) => setClientSecret(e.target.value)}
                className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm shadow-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                placeholder="••••••••••••••••••••••••"
                disabled={isSaving}
              />
              <p className="mt-1 text-xs text-gray-500">
                Your secret is stored securely in the OS Credential Vault and never leaves your machine.
              </p>
            </div>
          </div>

          {error && (
            <div className="mt-4 rounded-md bg-red-50 p-3 text-sm text-red-700">
              {error}
            </div>
          )}

          {/* Delete Confirmation */}
          {showDeleteConfirm && (
            <div className="mt-4 rounded-md bg-red-50 p-3 border border-red-200">
              <p className="text-sm text-red-800 font-medium">Are you sure you want to delete these credentials?</p>
              <p className="text-xs text-red-600 mt-1">This will disconnect your account and remove the keys permanently.</p>
              <div className="mt-3 flex space-x-2">
                <button
                  type="button"
                  onClick={async () => {
                    const success = await onDelete();
                    if (success) onClose();
                  }}
                  className="rounded bg-red-600 px-3 py-1 text-xs text-white hover:bg-red-700"
                >
                  Yes, Delete Keys
                </button>
                <button
                  type="button"
                  onClick={() => setShowDeleteConfirm(false)}
                  className="rounded border border-gray-300 px-3 py-1 text-xs text-gray-700 hover:bg-gray-50"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          {/* Footer */}
          <div className="mt-6 flex items-center justify-between">
            <div>
              {existingClientId && !showDeleteConfirm && (
                <button
                  type="button"
                  onClick={() => setShowDeleteConfirm(true)}
                  className="text-sm text-red-600 hover:text-red-800 font-medium"
                >
                  Delete Keys
                </button>
              )}
            </div>
            <div className="flex items-center space-x-3">
              <button
                type="button"
                onClick={onClose}
                disabled={isSaving}
                className="rounded-md border border-gray-300 bg-white px-4 py-2 text-sm font-medium text-gray-700 shadow-sm hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={isSaving || !clientId.trim() || (!clientSecret.trim() && !existingClientId)}
                className="inline-flex items-center rounded-md border border-transparent bg-blue-600 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 disabled:opacity-50"
              >
                {isSaving ? (
                  <>
                    <svg className="mr-2 h-4 w-4 animate-spin text-white" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                    </svg>
                    Saving...
                  </>
                ) : existingClientId ? (
                  "Update Credentials"
                ) : (
                  "Save & Test Connection"
                )}
              </button>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}
