# Docker installation

A containerised version of the psa_car_controller.
- Docker Hub: https://hub.docker.com/r/flobz/psa_car_controller

### Overview
Once the container is running, the configuration of the psa_car_controller app is near-identical classic Linux/Windows installtion.

### Installation
Create the container, detached, exposing port 5000, and mapping config folder on your host to /config inside the container:

#### With docker-compose
```
docker-compose up -d
```
#### With Docker:
```
docker run -d -ti --name psa_car_controller1 \
  --publish 5000:5000 \
  -v /host_path/config:/config \
  --restart unless-stopped \
  flobz/psa_car_controller
```
Go to http://127.0.0.1:5000 and follow instruction

### Environment variable:
You can modify some parameter with docker environment variable:

|variable         | description                     |
|-----------------|---------------------------------|
|PSACC_CONFIG_DIR | overide configuration directory |
|PSACC_PORT       | change port                     |
|PSACC_OPTIONS    | add cli argurment               |
|PSACC_GITHUB_TOKEN | optional, GitHub token for the version check of the application file, above 60 requests per hour |

### Build arguments:
A build from a branch has no release version. These optional arguments of `docker build` name its commit in the header, in Settings and in the log. A release ignores them.

|argument           | description                                                                                 |
|-------------------|---------------------------------------------------------------------------------------------|
|PSACC_GIT_VERSION  | output of `git describe --tags --always --dirty`, letters, digits, `.`, `_` and `-` only     |
|PSACC_GIT_REPO_URL | `https://` URL of the repository holding that commit; without it the version has no link     |
|PSACC_BROWSER      | `false`: no Chromium and no playwright, see Slim image below (`true` by default)              |

Build the wheel in `dist/` from the same commit, else the image shows a commit it doesn't contain. Only pass the URL if the commit is pushed, else the link ends on a 404. Without Docker, set the same names as environment variables.

### Slim image
Every image comes in two variants: `4.0.1-next` and `4.0.1-next-slim`, `next` and `next-slim`, `latest` and
`latest-slim`, `v4.0.0` and `v4.0.0-slim` and so on. The slim
one has no Chromium and no playwright: 854 MB instead of 1.92 GB (arm64), for small machines such as a Raspberry Pi.
Everything works the same, except the automatic login in a local browser: the login goes through a
[login worker](login_worker.md) on another machine, or the manual link (open it in a browser on your PC and paste the
code). A login is only needed once, and again when Stellantis rejects the stored refresh token.

To build it yourself: `docker build --build-arg PSACC_BROWSER=false .`

### Versioned next images

To publish a version of `next`, tag the chosen commit and push the tag to the fork:

```sh
git tag next-v4.0.1 <commit>
git push fork next-v4.0.1
```

The branch image workflow tests that commit and publishes both amd64 and arm64 images as
`ghcr.io/gsparber/psa_car_controller:4.0.1-next` and `ghcr.io/gsparber/psa_car_controller:4.0.1-next-slim`,
and moves `next` and `next-slim` to them. Use a new version tag for each published update.

Only such a tag or a manual run of the workflow (Actions → "Publish branch Docker image to GHCR" → Run workflow)
builds images. A push to a branch runs the tests only. A manual run on a branch publishes `<branch>` and
`<branch>-slim` (e.g. `feature-modern-ui`), and the same with the commit, e.g. `feature-modern-ui-a1b2c3d`.

For Renovate, use the versioned image in your Compose file:

```yaml
services:
  psacc:
    image: ghcr.io/gsparber/psa_car_controller:4.0.1-next
```

Renovate's default [Docker versioning](https://docs.renovatebot.com/modules/versioning/docker/) recognizes
the numeric version and keeps the suffix: `4.0.1-next` can update to `4.0.2-next`, and `4.0.1-next-slim`
to `4.0.2-next-slim`. No custom versioning rule is needed. The prefixes `next-v4.0.1` and `next4.0.1`
would require a custom Renovate rule if used as Docker tags. The unversioned `next` alias supports digest
updates instead of version updates.
