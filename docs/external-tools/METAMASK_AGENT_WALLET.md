# MetaMask Agent Wallet

## Papel en el proyecto

MetaMask Agent Wallet es la capa externa de interacción con Hyperliquid mediante
la CLI `mm`. Se mantiene separada del análisis Python. Market State, Scanner,
Analysis Pipeline y Fast Revalidation no la invocan ni necesitan sus credenciales.

La herramienta puede consultar posiciones y también realizar operaciones que
modifican estado. Tener esas capacidades disponibles no significa que el
pipeline las utilice ni que una propuesta del modelo autorice su ejecución.

## Configuración de trabajo: server-wallet + guard

La configuración indicada para las primeras pruebas del proyecto es
`server-wallet + guard`, con autenticación mediante QR y MetaMask Mobile.
El terminal presenta un QR y el usuario aprueba la conexión desde la aplicación.
El canal de aprobación depende del método de inicio de sesión.
Consulta la [configuración oficial](https://docs.metamask.io/agent-wallet/cli-setup/).

En `server-wallet`, el material de firma se gestiona en el servicio, en un
entorno TEE; el agente no recibe directamente la mnemonic personal.
La contrapartida es depender de ese servicio y proteger la sesión autenticada.
Esta separación no garantiza ausencia de pérdidas.
Consulta la [arquitectura oficial](https://docs.metamask.io/agent-wallet/reference/architecture/).

`guard` añade políticas y aprobaciones para operaciones fuera de sus límites.
Su eficacia depende de la política efectiva y del tipo de operación.
No sustituye la revisión humana ni valida una estrategia financiera.
Consulta los [modos oficiales](https://docs.metamask.io/agent-wallet/reference/trading-modes/).

Las primeras pruebas utilizaron esta combinación para evitar introducir la
mnemonic personal en el entorno del agente y conservar una capa de políticas
y aprobaciones mientras se estudiaba la interacción. Es una elección para esta
fase del proyecto, no una recomendación universal.

## server-wallet y BYOK

| Opción | Ventajas para este proyecto | Inconvenientes |
|---|---|---|
| `server-wallet` | Separa el material de firma de la wallet personal y permite apoyarse en políticas del servicio | Dependencia del servicio, autenticación y flujos de aprobación; una sesión comprometida sigue siendo un riesgo |
| BYOK (`byok`) | Control local directo del usuario sobre el material de firma | Mayor exposición operativa si la mnemonic entra en un entorno automatizado; más responsabilidad en su almacenamiento y uso |

BYOK significa aportar una mnemonic para operar con control local. No se
documentan aquí procedimientos para introducirla ni ejemplos de secretos.
Ninguna opción es universalmente mejor: la elección depende del entorno y del
modelo de permisos.

## Comandos del flujo de trabajo

La consulta de posiciones registrada en el contexto de trabajo del proyecto es:

```bash
mm perps positions --venue hyperliquid --network mainnet --toon
```

Es una consulta de lectura independiente del pipeline. Su salida puede contener
direcciones y datos de cuenta; no debe copiarse sin revisar a documentación
pública. No se añade aquí un catálogo de comandos de ejecución.

El mecanismo de autenticación QR corresponde a `mm login qr` y la selección
de configuración a `mm init`, según la documentación oficial. Se citan para
explicar la configuración, no como evidencia de que el pipeline los ejecute.
No se publica configuración local, identificadores de sesión ni datos de wallet.

## Alcance de la verificación

La configuración y el uso inicial descritos proceden del contexto del proyecto.
Esta reorganización no inicia sesión, no consulta wallets ni verifica el estado
actual de una sesión o de sus políticas. No hay una bitácora pública completa
de comandos ejecutados en el repositorio.

La [documentación oficial de MetaMask Agent Wallet](https://docs.metamask.io/agent-wallet/)
es la referencia para instalación, comandos y condiciones vigentes.
Antes de cualquier uso operativo, revisar también [Seguridad](../SECURITY.md).
