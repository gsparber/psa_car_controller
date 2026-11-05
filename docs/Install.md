# Installation

1.1 Download the app on your computer. 

1.2 Install requirements :

- You need **python >= 3.15**

- Then :
      ```pip3 install psa-car-controller```

- Install [Playwright](https://playwright.dev/python/) for automatic OAuth login:

  ```bash
  pip3 install playwright
  playwright install --with-deps --only-shell chromium
  ```

  > If Playwright is not installed or the browser fails to start, automatic login falls back to the manual flow.
  > If the browser gets rejected, you can use [your own login worker](login_worker.md).


1.3 start the app:
        
Start the app with charge control enabled :

``psa-car-controller --web-conf``

At the first launch you will be asked to connect and give a code that you will receive by SMS and also give your pin code (the four-digit code that your use on the android app).
If it failed you can remove the file otp.bin and retry.

You can see all options available with :
``psa-car-controller -h``
