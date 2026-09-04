"""
Утилиты для логирования.
Настраивает цветное логирование для приложения.
"""
import logging
import colorlog


def setup_logger(name: str = "bot", level: int = logging.INFO) -> logging.Logger:
    """
    Настраивает и возвращает логгер с цветным выводом.
    
    Args:
        name: Имя логгера
        level: Уровень логирования
        
    Returns:
        Настроенный логгер
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    # Если уже есть обработчики, не добавляем новые
    if logger.handlers:
        return logger
    
    # Создаем форматтер с цветами
    formatter = colorlog.ColoredFormatter(
        "%(log_color)s%(levelname)-8s%(reset)s %(blue)s%(name)s%(reset)s: %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            "DEBUG": "cyan",
            "INFO": "green",
            "WARNING": "yellow",
            "ERROR": "red",
            "CRITICAL": "bold_red",
        },
    )
    
    # Создаем обработчик консоли
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    
    # Добавляем обработчик к логгеру
    logger.addHandler(console_handler)
    
    return logger
