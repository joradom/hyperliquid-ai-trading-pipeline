# Hyperliquid AI Trading Research Pipeline

Proyecto experimental para investigar un flujo de análisis de mercados perpetuos
de Hyperliquid semi-automatizado con IA.

Combina captura y validación de datos con Python, filtrado determinista mediante
scanner, análisis mediante modelos de IA y una capa separada de interacción con
Hyperliquid mediante MetaMask Agent Wallet.

Es experimental: no es un producto terminado ni un bot autónomo de trading.
Gran parte de la arquitectura actual es de solo lectura (`read-only`).
Algunas capas futuras podrían interactuar con fondos reales.

## Documentación

- [Guía de uso](docs/USAGE.md)
- [Arquitectura](docs/ARCHITECTURE.md)
- [Componentes](docs/components/)
- [Herramientas externas](docs/external-tools/)
- [Seguridad](docs/SECURITY.md)
- [DISCLAIMER](DISCLAIMER.md)

## Aviso

> Este proyecto contiene código experimental relacionado con mercados financieros, criptomonedas, wallets y automatización. Puede contener errores y provocar pérdidas económicas. Antes de utilizar cualquier parte del proyecto, lee el [DISCLAIMER](DISCLAIMER.md).

## Licencia

Este proyecto se distribuye bajo GNU General Public License v3.0.
Consulta [LICENSE.md](LICENSE.md) para el texto completo. GPLv3 permite estudiar,
copiar, utilizar, modificar y redistribuir el software conforme a sus términos,
incluidas sus obligaciones de copyleft y de proporcionar el código fuente
cuando corresponda.

El software se proporciona sin garantías en la medida permitida por la
legislación aplicable. Es experimental y puede provocar pérdidas financieras
o de activos. Consulta [DISCLAIMER.md](DISCLAIMER.md),
[docs/SECURITY.md](docs/SECURITY.md) y el aviso informativo sobre versiones
modificadas en [NOTICE.md](NOTICE.md).
