#!/bin/sh
# Generate the TLS material the HTTPS listener needs.
#
# WHY HTTPS AT ALL: the SPL meter uses the phone's microphone, and getUserMedia
# only works in a secure context. iOS Safari refuses mic access over plain http
# (localhost excepted), so the dashboard has to offer https for that one feature.
#
# Two certificates, deliberately:
#   certs/ca.crt      a small root CA — this is what you install on the iPhone
#   certs/server.crt  the leaf it signs, carrying the SAN entries iOS validates
# Keeping them separate means a new LAN IP only needs a new leaf (re-run this),
# NOT a re-install on the phone. A single self-signed cert would force the whole
# trust dance again every time DHCP moved the machine.
#
# iOS 13+ rejects a server cert unless it has SAN entries (CN alone is ignored),
# is SHA-256, carries EKU serverAuth, and lasts <= 398 days. All set below.
set -e
cd "$(dirname "$0")"
mkdir -p certs
chmod 700 certs

HOST=$(scutil --get LocalHostName 2>/dev/null || hostname)
IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo 127.0.0.1)
SAN="DNS:${HOST}.local,DNS:${HOST},DNS:localhost,IP:${IP},IP:127.0.0.1"

if [ ! -f certs/ca.crt ]; then
  echo "==> new root CA (install certs/ca.crt on the phone, once)"
  openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
    -keyout certs/ca.key -out certs/ca.crt \
    -subj "/CN=MB HiFi Dashboard CA/O=MB HiFi" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
else
  echo "==> reusing existing root CA (no phone re-install needed)"
fi

echo "==> leaf for ${SAN}"
openssl req -newkey rsa:2048 -sha256 -nodes \
  -keyout certs/server.key -out certs/server.csr \
  -subj "/CN=${HOST}.local/O=MB HiFi" 2>/dev/null

cat > certs/leaf.ext <<EXT
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=${SAN}
EXT

openssl x509 -req -in certs/server.csr -CA certs/ca.crt -CAkey certs/ca.key \
  -CAcreateserial -out certs/server.crt -days 397 -sha256 \
  -extfile certs/leaf.ext 2>/dev/null

rm -f certs/server.csr certs/leaf.ext
chmod 600 certs/*.key
echo
echo "done. names on this cert:"
openssl x509 -in certs/server.crt -noout -ext subjectAltName | tail -1
echo
echo "next: restart the dashboard, then on the iPhone open"
echo "  http://${HOST}.local:8765/ca.crt   (install profile, then trust it in"
echo "  Settings > General > About > Certificate Trust Settings)"
