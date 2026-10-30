```markdown
# mTLS Setup Guide — PELDRUN MCP

This guide explains how to configure Mutual TLS (mTLS) for Enterprise
MCP servers in PELDRUN. It covers manifest configuration, generating
test certificates, and troubleshooting common errors.

**Target Audience:** Enterprise operators, DevOps engineers, and
anyone connecting PELDRUN to an MCP server that requires client
certificates.

---

## 1. Overview

### 1.1 What is mTLS?

Standard TLS (HTTPS) authenticates the **server** to the **client**
(your browser verifies the website's certificate). Mutual TLS (mTLS)
adds a second layer: the **client** must also present a valid
certificate to the **server**.

This is the standard authentication mechanism for:
- Banking and financial APIs
- Healthcare data exchange (HIPAA)
- Government and defense networks
- Zero-trust internal microservices

### 1.2 How PELDRUN Handles mTLS

PELDRUN **does not embed certificates** in manifest files. Instead:

1. The manifest declares **which environment variables** hold the
   filesystem paths to the certificate, private key, and CA bundle.
2. The user enters the **absolute file paths** in the Settings UI.
3. At connection time, PELDRUN reads the files and passes them to
   `httpx.AsyncClient(cert=..., verify=...)`.

**Security invariant:** Certificates and private keys never leave
the filesystem where they are stored. The encrypted settings file
only contains the path string (e.g., `C:\certs\client.pem`), not the
certificate itself.

---

## 2. Manifest Configuration

An mTLS manifest requires three elements:

1. `auth_strategy: "mtls"`
2. An `mtls` block mapping logical roles to environment variable names.
3. An `env_schema` that prompts the user for the paths.

### 2.1 Minimal Example

```json
{
  "id": "my_enterprise_mcp",
  "name": "Enterprise API",
  "transport": "http",
  "url": "https://internal.example.com/mcp",
  "auth_strategy": "mtls",
  "mtls": {
    "cert_env": "MTLS_CLIENT_CERT",
    "key_env": "MTLS_CLIENT_KEY",
    "ca_env": "MTLS_CA_BUNDLE"
  },
  "env_schema": {
    "type": "object",
    "required": ["MTLS_CLIENT_CERT", "MTLS_CLIENT_KEY"],
    "properties": {
      "MTLS_CLIENT_CERT": {
        "type": "string",
        "title": "Client Certificate Path",
        "description": "Absolute path to the .pem or .crt file",
        "secret": true
      },
      "MTLS_CLIENT_KEY": {
        "type": "string",
        "title": "Client Private Key Path",
        "description": "Absolute path to the unencrypted .pem or .key file",
        "secret": true
      },
      "MTLS_CA_BUNDLE": {
        "type": "string",
        "title": "Custom CA Bundle (Optional)",
        "description": "Absolute path to a CA bundle if using a private PKI",
        "secret": false
      }
    }
  }
}
```

### 2.2 Field Rules

| Field | Required | Meaning |
|---|---|---|
| `cert_env` | **Yes** | Env var name holding the client certificate path |
| `key_env` | No | Env var name holding the private key path. If empty, `cert_env` must point to a combined cert+key `.pem` file |
| `ca_env` | No | Env var name holding the custom CA bundle path. If empty, PELDRUN uses the system trust store |

**Important:** The `mtls` block is **silently ignored** if
`transport` is `"stdio"`. Subprocesses cannot participate in a TLS
handshake with the parent. Only `"http"` and `"sse"` transports
support mTLS.

---

## 3. Generating Test Certificates

For development and testing, you can generate a self-signed CA and
client certificate using `openssl`. These commands work on Linux,
macOS, and Windows (with OpenSSL installed, e.g., via Git Bash).

### 3.1 Step 1: Create a Certificate Authority (CA)

```bash
# Generate CA private key
openssl genpkey -algorithm RSA -out ca-key.pem -pkeyopt rsa_keygen_bits:2048

# Generate self-signed CA certificate (valid for 10 years)
openssl req -x509 -new -nodes -key ca-key.pem -sha256 -days 3650 \
  -out ca-cert.pem \
  -subj "/C=US/ST=State/L=City/O=Test CA/CN=test-ca.local"
```

### 3.2 Step 2: Create a Client Certificate

```bash
# Generate client private key
openssl genpkey -algorithm RSA -out client-key.pem -pkeyopt rsa_keygen_bits:2048

# Generate client Certificate Signing Request (CSR)
openssl req -new -key client-key.pem \
  -out client.csr \
  -subj "/C=US/ST=State/L=City/O=PELDRUN Client/CN=peldrun-client"

# Sign the client certificate with the CA
openssl x509 -req -in client.csr -CA ca-cert.pem -CAkey ca-key.pem \
  -CAcreateserial -out client-cert.pem -days 365 -sha256
```

### 3.3 Step 3: Verify the Certificates

```bash
# Verify that the client cert was signed by the CA
openssl verify -CAfile ca-cert.pem client-cert.pem
# Expected output: client-cert.pem: OK
```

You should now have three files:

| File | Purpose | Env Var |
|---|---|---|
| `client-cert.pem` | Client certificate | `MTLS_CLIENT_CERT` |
| `client-key.pem` | Client private key | `MTLS_CLIENT_KEY` |
| `ca-cert.pem` | CA bundle | `MTLS_CA_BUNDLE` |

### 3.4 Step 4: Combine Cert and Key (Optional)

If your server expects a single combined `.pem` file instead of
separate cert and key files:

```bash
cat client-cert.pem client-key.pem > client-combined.pem
```

Then set:
- `MTLS_CLIENT_CERT` = path to `client-combined.pem`
- Leave `MTLS_CLIENT_KEY` empty

---

## 4. Testing with a Local mTLS Server

Before configuring PELDRUN, verify your certificates work against a
real TLS endpoint.

### 4.1 Using `openssl s_client`

```bash
openssl s_client -connect internal.example.com:443 \
  -cert client-cert.pem \
  -key client-key.pem \
  -CAfile ca-cert.pem
```

If the handshake succeeds, you will see:
```
Verify return code: 0 (ok)
```

### 4.2 Using `curl`

```bash
curl --cert client-cert.pem \
     --key client-key.pem \
     --cacert ca-cert.pem \
     https://internal.example.com/mcp
```

A `200 OK` or a valid JSON-RPC response confirms the certs are
accepted by the server.

### 4.3 Using Python

```python
import httpx

with httpx.Client(
    cert=("client-cert.pem", "client-key.pem"),
    verify="ca-cert.pem",
) as client:
    r = client.post("https://internal.example.com/mcp", json={...})
    print(r.status_code)
```

If all three succeed, the certificates are valid. Proceed to §5.

---

## 5. Configuring PELDRUN

### 5.1 Add the Manifest

**Option A — From the Store UI:**
1. Open **Store → Extensions**.
2. Click **Upload MCP (.json / .zip)**.
3. Select your mTLS manifest JSON.

**Option B — Drop in builtins:**
1. Place the JSON in `backend/omweb/extensions/builtins/`.
2. Restart the backend.

The server appears in the extensions list with status `ready`.

### 5.2 Configure Certificate Paths

1. Click the **Settings** gear icon on the new server.
2. Switch to the **Environment Variables** tab.
3. Enter the **absolute** paths to your certificates:

| Field | Example (Windows) | Example (Linux/macOS) |
|---|---|---|
| Client Certificate Path | `C:\certs\client-cert.pem` | `/etc/certs/client-cert.pem` |
| Client Private Key Path | `C:\certs\client-key.pem` | `/etc/certs/client-key.pem` |
| Custom CA Bundle | `C:\certs\ca-cert.pem` | `/etc/certs/ca-cert.pem` |

4. Click **Save Configuration**.

**Windows path note:** Use forward slashes (`C:/certs/...`) or double
backslashes (`C:\\certs\\...`). Single backslashes are valid JSON
escapes and will corrupt the path.

### 5.3 Activate the Server

1. Toggle the server from `ready` to `active`.
2. The status badge turns green.

### 5.4 Verify the Connection

1. Click **Test Connection**.
2. Expected: **"Successfully connected... Discovered N tool(s)"**.

If the handshake fails, see §6.

---

## 6. Troubleshooting

### 6.1 Common Errors

| Error Message | Cause | Fix |
|---|---|---|
| `SSLError: [SSL: CERTIFICATE_VERIFY_FAILED]` | Server rejected the client cert, or CA bundle doesn't match | Verify the cert chain: `openssl verify -CAfile ca-cert.pem client-cert.pem` |
| `SSLError: [Errno 2] No such file or directory` | Certificate path is wrong or file is not readable | Check the path; try reading the file manually: `cat /path/to/cert.pem` |
| `SSLError: PEM lib` | The `.pem` file is malformed | Regenerate the cert; ensure no trailing whitespace or BOM |
| `TypeError: cert must be str, bytes, or tuple` | Path is empty or not a string | Ensure the env var is set to a non-empty string |
| `Connection timed out` | TLS handshake succeeded but server is not responding | Verify the server URL; test with `curl --cert ...` |
| `HTTP 401 Unauthorized` | Cert accepted, but server also requires a Bearer token | Combine mTLS with `auth_strategy: "oauth"` (rare; most mTLS servers don't need tokens) |

### 6.2 No mTLS Prompt Appears

If the Settings modal shows no fields:

- Check that `env_schema` is defined in the manifest.
- Check that the `mtls` block is present.
- Restart the backend if you added the manifest manually.

### 6.3 "Client Certificate Path" Is Empty After Save

**Symptom:** You entered a path, clicked Save, but the field is empty
on reload.

**Cause:** The `env_schema` field is marked `"secret": true`.
Encrypted secrets are re-displayed only as a placeholder on reload,
not the actual value. This is intentional — the backend **does**
have the path; it just does not return it via the settings endpoint.

**Verification:** Click **Test Connection**. If the connection
succeeds, the path is stored correctly.

### 6.4 Cert Works with `curl` but Not with PELDRUN

Possible causes:

1. **Encrypted private key.** PELDRUN passes the key directly to
   `httpx`, which does not prompt for a passphrase. If your key is
   encrypted (`-----BEGIN ENCRYPTED PRIVATE KEY-----`), decrypt it
   first:
   ```bash
   openssl pkey -in encrypted-key.pem -out client-key.pem
   ```
2. **Path with spaces not quoted.** On Windows, `C:\Program Files\certs\client.pem`
   must be quoted in some contexts. Use forward slashes:
   `C:/Program Files/certs/client.pem`.
3. **CA bundle mismatch.** PELDRUN uses the `ca_env` path as the
   **exclusive** trust store. If your server uses a public CA (e.g.,
   Let's Encrypt) in addition to your private CA, you must concatenate
   both bundles:
   ```bash
   cat /etc/ssl/certs/ca-certificates.crt my-private-ca.pem > combined-ca.pem
   ```

---

## 7. Security Best Practices

1. **Never commit certificates to git.** Add `*.pem` and `*.key` to
   `.gitignore`.
2. **Restrict file permissions.** On POSIX systems:
   ```bash
   chmod 600 client-key.pem
   chmod 644 client-cert.pem ca-cert.pem
   ```
3. **Use separate keys per environment.** Do not reuse production
   certificates in development.
4. **Rotate certificates before expiry.** Set a calendar reminder for
   `openssl x509 -in client-cert.pem -noout -enddate`.
5. **Store the private key on an encrypted volume** (BitLocker, LUKS,
   FileVault) if the machine is shared.
6. **Monitor `SSLError` rates.** A spike suggests a certificate has
   expired or been revoked.
7. **The manifest does not contain secrets.** It only declares env
   var names. The actual paths are stored encrypted by PELDRUN's
   `secret_storage.py`.

---

## 8. Reference: Minimal Working Example

Complete flow to test mTLS end-to-end with a local server.

### 8.1 Start a Local mTLS Server (Python)

```python
# save as mtls_server.py
import ssl
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI()

@app.post("/mcp")
async def mcp_endpoint():
    # Minimal JSON-RPC response for tools/list
    return JSONResponse({
        "jsonrpc": "2.0",
        "id": "1",
        "result": {"tools": [
            {"name": "echo", "description": "Test tool", "inputSchema": {}}
        ]}
    })

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8443,
        ssl_keyfile="server-key.pem",
        ssl_certfile="server-cert.pem",
        ssl_ca_certs="ca-cert.pem",
        ssl_cert_reqs=ssl.CERT_REQUIRED,   # ← mTLS enforcement
    )
```

### 8.2 Manifest

```json
{
  "id": "test_mtls_local",
  "name": "Local mTLS Test",
  "type": "mcp_server",
  "transport": "http",
  "url": "https://127.0.0.1:8443/mcp",
  "auth_strategy": "mtls",
  "mtls": {
    "cert_env": "MTLS_CLIENT_CERT",
    "key_env": "MTLS_CLIENT_KEY",
    "ca_env": "MTLS_CA_BUNDLE"
  },
  "env_schema": {
    "type": "object",
    "required": ["MTLS_CLIENT_CERT", "MTLS_CLIENT_KEY"],
    "properties": {
      "MTLS_CLIENT_CERT": {"type": "string", "secret": true, "title": "Client Cert Path"},
      "MTLS_CLIENT_KEY":  {"type": "string", "secret": true, "title": "Client Key Path"},
      "MTLS_CA_BUNDLE":   {"type": "string", "secret": false, "title": "CA Bundle Path"}
    }
  }
}
```

### 8.3 Expected Outcome

- **Test Connection** → "Successfully connected. Discovered 1 tool(s)."
- **Manage Tools** → shows "echo" tool.
- **Bash loopback test** → `curl --cert client-cert.pem --key client-key.pem --cacert ca-cert.pem https://127.0.0.1:8443/mcp` returns the same JSON.

---

## 9. Related Documentation

- **Manifest reference:** `docs/MCP_MANIFEST_REFERENCE.md`
- **Auth deep-dive:** `docs/MCP_AUTH.md`
- **Roadmap:** `docs/MCP_ROADMAP.md`
- **httpx mTLS:** https://www.python-httpx.org/advanced/#client-instances
- **OpenSSL docs:** https://www.openssl.org/docs/

---

*Last updated: post Phase B implementation. Reference manifest at
`backend/omweb/extensions/builtins/test_mtls.json`.*
```
