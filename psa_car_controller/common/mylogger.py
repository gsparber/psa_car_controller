import logging
from logging.handlers import RotatingFileHandler

from psa_car_controller.common.sensitive_filter import sensitive_filter

LOG_FILE = 'activity.log'

DEBUG_LEVELV_NUM = 9
logging.addLevelName(DEBUG_LEVELV_NUM, "DEBUGV")


class CustomLogger(logging.Logger):
    def _log(self, level,  # pylint: disable=too-many-arguments,unused-argument,too-many-positional-arguments
             msg,
             args,
             exc_info=None,
             extra=None,
             stack_info=False,
             stacklevel=1,
             exc_info_debug=False,
             **kwargs):
        if exc_info_debug and self.isEnabledFor(logging.DEBUG):
            exc_info = True
        super()._log(level, msg, args, exc_info, extra, stack_info, stacklevel)

    # pylint: disable-next=too-many-arguments,too-many-positional-arguments
    def __new_style_log(self, level, msg, args, exc_info=None, extra=None,
                        stack_info=False, **kwargs):
        if kwargs.pop('style', "%") == "{":  # optional
            msg = msg.format(*args)
            args = []
        self._log(level, msg, args, exc_info, extra, stack_info)

    def debugv(self, msg, *args, **kwargs):
        if self.isEnabledFor(DEBUG_LEVELV_NUM):
            self.__new_style_log(DEBUG_LEVELV_NUM, msg, args, **kwargs)

    @staticmethod
    def getLogger(name):
        return logging.getLogger(name)


logging.setLoggerClass(CustomLogger)
logger = logging.getLogger()

file_handler = RotatingFileHandler(LOG_FILE, 'a', 1000000, 1, encoding='utf8')
formatter = logging.Formatter('%(asctime)s :: %(levelname)s :: %(message)s')
stream_handler = logging.StreamHandler()
file_handler.addFilter(sensitive_filter)
stream_handler.addFilter(sensitive_filter)


def my_logger(handler_level=logging.INFO):
    logger.setLevel(handler_level)
    file_handler.setLevel(handler_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    stream_handler.setLevel(handler_level)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    # werkzeug logs HTTP access lines at INFO; only show them in debug mode
    werkzeug_logger = logging.getLogger("werkzeug")
    werkzeug_logger.setLevel(handler_level if handler_level <= logging.DEBUG else logging.WARNING)
