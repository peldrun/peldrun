// frontend/src/features/source-control/components/ProviderConnectionCard.tsx

import React from "react";
import { ProviderDescriptorResponse, ConnectionStatusResponse } from "../api/schemas";
import { formatGrantedScopes } from "../utils/permission-labels";

interface ProviderConnectionCardProps {
  provider: ProviderDescriptorResponse;
  connection?: ConnectionStatusResponse;
  isConnecting: boolean;
  onConnect: (providerId: string) => void;
  onDisconnect: (providerId: string) => void;
  onValidate: (providerId: string) => void;
  onConfigure: (provider: ProviderDescriptorResponse) => void;
}

/**
 * A presentational card displaying the connection status and capabilities
 * of a specific source control provider (e.g., GitHub, GitLab).
 * Handles the "Not Configured" state for local-first setups.
 */
export function ProviderConnectionCard({
  provider,
  connection,
  isConnecting,
  onConnect,
  onDisconnect,
  onValidate,
  onConfigure,
}: ProviderConnectionCardProps) {
  const isConnected = connection?.is_connected ?? false;
  const isConfigured = provider.is_configured ?? false;

  return (
    <div className="flex flex-col rounded-lg border border-gray-200 bg-white shadow-sm transition-shadow hover:shadow-md">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-gray-100 p-4">
        <div className="flex items-center space-x-3">
          {/* Placeholder for Provider Logo */}
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-gray-100 text-lg font-bold text-gray-600">
            {provider.display_name.charAt(0).toUpperCase()}
          </div>
          <div>
            <h3 className="text-lg font-medium text-gray-900">{provider.display_name}</h3>
            <p className="text-sm text-gray-500">{provider.api_base_url}</p>
          </div>
        </div>

        <div className="flex items-center space-x-2">
          {/* Settings Button */}
          <button
            type="button"
            onClick={() => onConfigure(provider)}
            className="rounded-full p-2 text-gray-400 hover:bg-gray-100 hover:text-gray-600 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2"
            title="Configure OAuth App Credentials"
          >
            <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
            </svg>
          </button>

          {/* Status Badge */}
          <span
            className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${
              isConnected
                ? "bg-green-100 text-green-800"
                : "bg-gray-100 text-gray-800"
            }`}
          >
            {isConnected ? "Connected" : "Not Connected"}
          </span>
        </div>
      </div>

      {/* Body */}
      <div className="flex-1 p-4">
        {!isConfigured && !isConnected ? (
          <div className="rounded-md bg-yellow-50 p-3 border border-yellow-200">
            <div className="flex">
              <div className="flex-shrink-0">
                <svg className="h-5 w-5 text-yellow-400" viewBox="0 0 20 20" fill="currentColor">
                  <path fillRule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clipRule="evenodd" />
                </svg>
              </div>
              <div className="ml-3">
                <h3 className="text-sm font-medium text-yellow-800">Setup Required</h3>
                <div className="mt-2 text-sm text-yellow-700">
                  <p>
                    OAuth App credentials are missing. You must configure your own Client ID and Secret before connecting.
                  </p>
                </div>
                <div className="mt-3">
                  <button
                    type="button"
                    onClick={() => onConfigure(provider)}
                    className="text-sm font-medium text-yellow-800 underline hover:text-yellow-900"
                  >
                    Configure now
                  </button>
                </div>
              </div>
            </div>
          </div>
        ) : isConnected && connection ? (
          <div className="space-y-3">
            <div>
              <span className="text-sm font-medium text-gray-500">Account</span>
              <p className="text-sm text-gray-900">{connection.username || connection.account_id}</p>
            </div>

            <div>
              <span className="text-sm font-medium text-gray-500">Granted Permissions</span>
              <p className="text-sm text-gray-700">
                {formatGrantedScopes(connection.granted_scopes)}
              </p>
            </div>

            {connection.last_validated_at && (
              <div>
                <span className="text-sm font-medium text-gray-500">Last Validated</span>
                <p className="text-sm text-gray-700">
                  {new Date(connection.last_validated_at).toLocaleString()}
                </p>
              </div>
            )}
          </div>
        ) : (
          <p className="text-sm text-gray-500">
            Connect your {provider.display_name} account to allow the agent to read repositories,
            create branches, and request approvals for remote changes.
          </p>
        )}
      </div>

      {/* Footer / Actions */}
      <div className="flex items-center justify-end space-x-2 border-t border-gray-100 bg-gray-50 p-4 rounded-b-lg">
        {isConnected ? (
          <>
            <button
              type="button"
              onClick={() => onValidate(provider.provider_id)}
              className="rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 shadow-sm hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2"
            >
              Validate Connection
            </button>
            <button
              type="button"
              onClick={() => onDisconnect(provider.provider_id)}
              className="rounded-md border border-transparent bg-red-50 px-3 py-1.5 text-sm font-medium text-red-700 hover:bg-red-100 focus:outline-none focus:ring-2 focus:ring-red-500 focus:ring-offset-2"
            >
              Disconnect
            </button>
          </>
        ) : (
          <button
            type="button"
            onClick={() => onConnect(provider.provider_id)}
            disabled={isConnecting || !isConfigured}
            className="inline-flex items-center rounded-md border border-transparent bg-blue-600 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 disabled:bg-gray-300 disabled:text-gray-500 disabled:cursor-not-allowed"
          >
            {isConnecting ? (
              <>
                <svg className="mr-2 h-4 w-4 animate-spin text-white" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                </svg>
                Connecting...
              </>
            ) : !isConfigured ? (
              "Configuration Required"
            ) : (
              `Connect ${provider.display_name}`
            )}
          </button>
        )}
      </div>
    </div>
  );
}
