$ErrorActionPreference = "Stop"

throw @"
This swarm is installed and run on Debian only.
From Windows, use an SSH tunnel:
  ssh -L 8787:127.0.0.1:8787 <debian-user>@<debian-address>
Then open http://127.0.0.1:8787.
"@
