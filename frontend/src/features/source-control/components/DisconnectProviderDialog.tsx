// frontend/src/features/source-control/components/DisconnectProviderDialog.tsx

import React, { useState } from "react";

interface DisconnectProviderDialogProps {
  isOpen: boolean;
  providerName: string;
  isDisconnecting: boolean;
  onClose: () => void;
  onConfirm: (revokeUpstream: boolean) => void;
}

/**
 * A confirmation dialog for the destructive action of disconnecting a provider.
 * Warns the user that local credentials will be deleted and offers the option
 * to revoke the token on the provider's side as well.
 */
export function DisconnectProviderDialog({
  isOpen,
  providerName,
  isDisconnecting,
  onClose,
  onConfirm,
}: DisconnectProviderDialogProps) {
  // Default to true because revoking upstream is the safest option
  const [revokeUpstream, setRevokeUpstream] = useState(true);

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center overflow-y-auto bg-gray-900 bg-opacity-50 p-4">
      <div className="relative w-full max-w-md rounded-lg bg-white shadow-xl">

        {/* Header */}
        <div className="px-6 py-4">
          <div className="flex items-start">
            <div className="flex-shrink-0">
              <div className="flex h-10 w-10 items-center justify-center rounded-full bg-red-100">
                <svg className="h-6 w-6 text-red-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                </svg>
              </div>
            </div>
            <div className="ml-4">
              <h3 className="text-lg font-medium leading-6 text-gray-900">
                Disconnect {providerName}
              </h3>
              <div className="mt-2">
                <p className="text-sm text-gray-500">
                  Are you sure you want to disconnect your {providerName} account?
                  PELDRUN will no longer be able to interact with your repositories,
                  and all locally stored credentials will be permanently deleted from the system vault.
                </p>
              </div>
            </div>
          </div>
        </div>

        {/* Options */}
        <div className="bg-gray-50 px-6 py-4">
          <div className="flex items-start">
            <div className="flex h-5 items-center">
              <input
                id="revoke-upstream"
                name="revoke-upstream"
                type="checkbox"
                checked={revokeUpstream}
                onChange={(e) => setRevokeUpstream(e.target.checked)}
                disabled={isDisconnecting}
                className="h-4 w-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
              />
            </div>
            <div className="ml-3 text-sm">
              <label htmlFor="revoke-upstream" className="font-medium text-gray-700">
                Revoke access on {providerName}
              </label>
              <p className="text-gray-500">
                We highly recommend leaving this checked. It tells {providerName} to immediately
                invalidate the access token, preventing any other application from using it.
              </p>
            </div>
          </div>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end space-x-3 border-t border-gray-200 bg-gray-50 px-6 py-4 rounded-b-lg">
          <button
            type="button"
            onClick={onClose}
            disabled={isDisconnecting}
            className="rounded-md border border-gray-300 bg-white px-4 py-2 text-sm font-medium text-gray-700 shadow-sm hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => onConfirm(revokeUpstream)}
            disabled={isDisconnecting}
            className="inline-flex items-center rounded-md border border-transparent bg-red-600 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-red-700 focus:outline-none focus:ring-2 focus:ring-red-500 focus:ring-offset-2 disabled:opacity-50"
          >
            {isDisconnecting ? (
              <>
                <svg className="mr-2 h-4 w-4 animate-spin text-white" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                </svg>
                Disconnecting...
              </>
            ) : (
              "Disconnect Account"
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
