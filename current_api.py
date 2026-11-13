import os
import logging

import requests
from dotenv import load_dotenv


# Загружаем переменные из файла .env
load_dotenv()

# Получаем API-ключ из .env
API_KEY = os.getenv("EXCHANGE_API_KEY")

BASE_URL = "https://api.exchangerate.host"
logger = logging.getLogger(__name__)


def _redact_secrets(value):
    """Маскирует ключи в ответе API до передачи объекта в журнал."""
    secrets = (API_KEY, os.getenv("TELEGRAM_BOT_TOKEN"))
    if isinstance(value, dict):
        return {_redact_secrets(key): _redact_secrets(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
    return value


def check_api_key():
    """Проверяет, что API-ключ загружен из файла .env."""
    if not API_KEY:
        raise RuntimeError(
            "Не найден EXCHANGE_API_KEY. Проверь файл .env "
            "и убедись, что он лежит рядом с current_api.py."
        )


def make_api_request(endpoint, params):
    """Отправляет запрос к API и возвращает ответ в формате JSON."""
    check_api_key()

    # Не добавляем секрет в диагностические сообщения.
    safe_params = {
        key: "[REDACTED]" if str(key).lower() in {"access_key", "api_key", "token"} else _redact_secrets(value)
        for key, value in params.items()
    }
    params["access_key"] = API_KEY
    url = f"{BASE_URL}/{endpoint}"
    logger.info("API request endpoint=/%s params=%s", endpoint, safe_params)
    try:
        response = requests.get(url, params=params, timeout=20)
        logger.info("API response endpoint=/%s HTTP status=%s", endpoint, response.status_code)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        # Не печатаем текст исключения requests: URL запроса содержит access_key.
        logger.error("API request failed endpoint=/%s error_type=%s", endpoint, type(exc).__name__)
        raise RuntimeError(f"Не удалось выполнить запрос к API endpoint /{endpoint} ({type(exc).__name__}).") from None
    except ValueError:
        logger.exception("API returned invalid JSON endpoint=/%s", endpoint)
        raise RuntimeError(f"API вернул некорректный JSON для endpoint /{endpoint}.") from None

    safe_data = _redact_secrets(data)
    logger.info("API JSON endpoint=/%s response=%s", endpoint, safe_data)
    if not isinstance(data, dict):
        logger.error("API returned unexpected JSON type endpoint=/%s type=%s", endpoint, type(data).__name__)
        raise RuntimeError(f"API вернул неожиданный формат данных для endpoint /{endpoint}.")
    logger.info(
        "API fields endpoint=/%s success=%s info.quote=%s result=%s",
        endpoint,
        safe_data.get("success"),
        safe_data.get("info", {}).get("quote") if isinstance(safe_data.get("info"), dict) else None,
        safe_data.get("result"),
    )

    # Некоторые ошибки API приходят с HTTP-статусом 200,
    # поэтому отдельно проверяем поле success.
    if data.get("success") is False:
        error = data.get("error", {})
        if not isinstance(error, dict):
            error = {}
        code = error.get("code", "неизвестно")
        info = _redact_secrets(error.get("info", "Подробности ошибки не предоставлены."))
        logger.error("API reported failure endpoint=/%s code=%s info=%s", endpoint, code, info)
        raise RuntimeError(f"Ошибка API (код {code}): {info}")

    logger.info("API function make_api_request returned endpoint=/%s data=%s", endpoint, safe_data)
    return data


def get_current_rate(default: str = "USD",
                     currencies: tuple[str, ...] = ("EUR", "GBP", "JPY")):
    """Получает текущие курсы выбранных валют относительно базовой."""
    params = {
        "source": default,
        "currencies": ",".join(currencies),
    }

    return make_api_request("live", params)


def convert_currency(amount: float, from_currency: str, to_currency: str):
    """Конвертирует сумму из одной валюты в другую."""
    logger.info("convert_currency inputs amount=%s from=%s to=%s", amount, from_currency, to_currency)
    params = {
        "from": from_currency,
        "to": to_currency,
        "amount": amount,
    }

    result = make_api_request("convert", params)
    logger.info("convert_currency returned from=%s to=%s amount=%s result=%s", from_currency, to_currency, amount, result.get("result"))
    return result


def get_all_supported_currencies():
    """Получает список валют, поддерживаемых API."""
    return make_api_request("list", {})


if __name__ == "__main__":
    try:
        result = convert_currency(100, "USD", "VND")
        print("Результат конвертации:")
        print(result)
    except (requests.RequestException, RuntimeError, ValueError) as error:
        print(f"Не удалось выполнить запрос: {error}")
