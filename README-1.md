# 🧹 Bot de aseo de la casa (solo Telegram)

Reparte las tareas de la casa entre varias personas (funciona igual en iPhone y
Android). Cada quien marca su tarea con un botón: 🟢 si la cumple, ⬜ si está
pendiente. Lo no cumplido al final de la semana suma **$2.000 al tarro** de esa
persona. Las tareas se reparten **al azar** y son **permanentes** (siguen todas
las semanas). Cada noche llega un **recordatorio** de lo pendiente.

**No usa base de datos ni nada que mantener.** El estado se guarda dentro de
Telegram, en un mensaje fijado del grupo. Una vez montado, el bot corre solo.

---

## Comandos (dentro del grupo)

| Comando | Qué hace |
|---|---|
| `/registrarme` | Cada persona lo usa **una vez** |
| `/nueva lavar la loza` | Crea una tarea (eliges responsable o dejas que el azar decida) |
| `/repartir` | Reparte todas las tareas al azar y parejo |
| `/auto` | Activa/desactiva el reparto aleatorio automático de cada semana |
| `/tareas` | Ver y marcar tareas con botones |
| `/gestionar` | Eliminar (🗑️) o reasignar (🔄) tareas |
| `/renombrar 2 sacar la basura` | Renombra la tarea número 2 |
| `/comodin lavé el carro` | Pide perdón por una tarea; los demás **votan** |
| `/tarro` | Cuánto debe cada persona |
| `/saldar` | Registrar que alguien pagó (deja su saldo en $0) |
| `/cerrar` | Cierra la semana ahora mismo |

- **Reparto aleatorio:** `/repartir` reparte al instante, parejo. Con `/auto`
  activado (por defecto), cada semana se vuelve a repartir solo para que roten.
- **Tareas permanentes:** se crean una vez y siguen cada semana; se reinician
  solas y lo pendiente se cobra al tarro.
- **Recordatorio:** cada noche (por defecto después de las 7 p.m.) el bot avisa
  quién tiene tareas pendientes, mencionándolo. No tiene que ser puntual: sale
  la primera vez que alguien usa el bot esa noche.
- **Comodín (votación):** si no hiciste tu tarea pero hiciste otra cosa, usa
  `/comodin`; los demás aprueban 👍 o rechazan 👎.
- **Saldar:** `/saldar` muestra solo a quienes deben; al confirmar avisa cuánto
  pagó y deja su saldo en $0.

---

## Instalación (una sola vez — después no se toca más)

Solo necesitas **dos** cosas gratis, sin tarjeta.

### 1) Crear el bot en Telegram
1. Escríbele a **@BotFather**, manda `/newbot`, ponle nombre y un usuario que
   termine en `bot`.
2. Guarda el **token** (algo como `123456789:AA...`).

### 2) Poner el bot a correr (Render)
1. Sube esta carpeta a un repositorio en **GitHub**.
2. En **https://render.com** → **New → Web Service** → conecta el repo.
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python bot.py`
3. En **Environment**, agrega:
   - `BOT_TOKEN` → el token de BotFather
   - (opcional) `REMIND_HOUR` → hora del recordatorio en 24h (por defecto `19`)
4. **Create Web Service**.

### 3) Meter el bot al grupo
1. Crea un grupo con las personas.
2. Agrega el bot **y hazlo administrador** (necesita fijar el mensaje de estado).
3. Escribe `/start` en el grupo.
4. Cada uno manda `/registrarme`. Luego `/nueva ...` y `/repartir`. ¡Listo!

Después de esto no hay que hacer nada más. El bot funciona solo.

---

## Nota
No borren ni desanclen el mensaje fijado de estado: ahí viven las tareas y las
deudas. El bot debe seguir siendo administrador del grupo.
