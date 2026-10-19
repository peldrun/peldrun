// frontend/src/features/source-control/components/ProviderConnectDialog.tsx

import React from "react";
import { ProviderDescriptorResponse } from "../api/schemas";
import { getCapabilityInfo } from "../utils/permission-labels";

interface ProviderConnectDialogProps {
  isOpen: boolean;
  provider: ProviderDescriptorResponse | null;
  isConnecting: boolean;
  onClose: () => void;
  onConfirm: () => void;
}

/**
 * A pre-flight dialog shown to the user before initiating the OAuth flow.
 * It explains what permissions the agent will gain and warns about high-risk capabilities.
 */
export function ProviderConnectDialog({
  isOpen,
  provider,
  isConnecting,
  onClose,
  onConfirm,
}: ProviderConnectDialogProps) {
  if (!isOpen || !provider) return null;

  // Extract capabilities that are supported by this provider (value === true)
  const activeCapabilities = Object.entries(provider.capabilities)
    .filter(([_, isSupported]) => isSupported)
    .map(([key]) => {
      const info = getCapabilityInfo(key as any);
      return {
        key,
        ...info,
      };
    });

  const hasHighRiskCapability = activeCapabilities.some((c) => c.isHighRisk);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center overflow-y-auto bg-gray-900 bg-opacity-50 p-4">
      <div className="relative w-full max-w-lg rounded-lg bg-white shadow-xl">

        {/* Header */}
        <div className="border-b border-gray-200 px-6 py-4">
          <h2 className="text-xl font-semibold text-gray-900">
            Connect to {provider.display_name}
          </h2>
          <p className="mt-1 text-sm text-gray-500">
            You will be redirected to your system browser to authorize this connection.
          </p>
        </div>

        {/* Body */}
        <div className="px-6 py-4">
          <p className="mb-4 text-sm text-gray-700">
            By connecting your account, you grant PELDRUN the following technical capabilities.
            <span className="font-semibold text-gray-900"> Note: Every remote write still requires explicit manual approval.</span>
          </p>

          <div className="max-h-60 overflow-y-auto rounded-md border border-gray-200 bg-gray-50 p-3">
            <ul className="space-y-3">
              {activeCapabilities.map((cap) => (
                <li key={cap.key} className="flex items-start">
                  <div className="flex-shrink-0">
                    {cap.isHighRisk ? (
                      <span className="flex h-5 w-5 items-center justify-center rounded-full bg-red-100 text-red-600">
                        !
                      </span>
                    ) : (
                      <span className="flex h-5 w-5 items-center justify-center rounded-full bg-green-100 text-green-600">
                        ✓
                      </span>
                    )}
                  </div>
                  <div className="ml-3">
                    <p className={`text-sm font-medium ${cap.isHighRisk ? "text-red-700" : "text-gray-900"}`}>
                      {cap.label}
                    </p>
                    <p className="text-xs text-gray-500">{cap.description}</p>
                  </div>
                </li>
              ))}
            </ul>
          </div>

          {hasHighRiskCapability && (
            <div className="mt-4 rounded-md bg-red-50 p-3">
              <div className="flex">
                <div className="flex-shrink-0">
                  <svg className="h-5 w-5 text-red-400" viewBox="0 0 20 20" fill="currentColor">
                    <path fillRule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clipRule="evenodd" />
                  </svg>
                </div>
                <div className="ml-3">
                  <h3 className="text-sm font-medium text-red-800">High-Risk Permissions Requested</h3>
                  <p className="mt-1 text-xs text-red-700">
                    This integration requests capabilities that can alter your repository. Ensure you trust the agent's instructions.
                  </p>
                </div>
              </div>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end space-x-3 border-t border-gray-200 bg-gray-50 px-6 py-4 gap-2 rounded-b-lg">
          <button
            type="button"
            onClick={onClose}
            disabled={isConnecting}
            className="rounded-sm border border-gray-300 bg-white px-4 py-2 text-sm font-medium text-gray-700 shadow-sm hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={isConnecting}
            className="inline-flex items-center rounded-sm border border-transparent bg-rb m-2 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 disabled:opacity-50"
          >
            {isConnecting ? (
              <>
                <svg className="mr-2 h-4 w-4 animate-spin text-white" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                </svg>
                Opening Browser...
              </>
            ) : (
              "Agree & Continue to Browser"
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
