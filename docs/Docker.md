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

Build the wheel in `dist/` from the same commit, else the image shows a commit it doesn't contain. Only pass the URL if the commit is pushed, else the link ends on a 404. Without Docker, set the same names as environment variables.



