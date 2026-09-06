# CDRL — Base inicial del proyecto

Esta carpeta es la base común del proyecto **Cloud Data Reliability Lab (CDRL)** para la asignatura **Bases de datos en la nube**.

## Flujo de inicio

1. Descarga esta base desde Google Classroom.
2. Crea un repositorio GitHub propio para tu equipo; no trabajes sobre el repositorio del curso.
3. Copia el contenido de esta carpeta al repositorio del equipo.
4. Agrega únicamente a los integrantes del equipo, con un máximo de tres personas.
5. Ejecuta `make setup`, `make verify` y `make run`.
6. Completa el hito semanal y conserva evidencia técnica individual de tu contribución.

El lenguaje de la aplicación lo selecciona el equipo y debe documentarse en un ADR. La interfaz mínima común del repositorio es:

```text
make setup
make verify
make run
```

## Entornos

- AWS Academy Learner Lab es el entorno cloud oficial cuando el servicio esté habilitado.
- Docker Compose/PostgreSQL y el emulador local declarado por el equipo son el respaldo reproducible.
- No uses cuentas personales con facturación, ni subas credenciales, tokens o datos sensibles.

## Primera entrega

El hito M01 transforma esta base en un contrato de datos ejecutable: agrega el esquema relacional, migraciones idempotentes, seed sintético, pruebas, reporte y ADR. La base inicial solamente verifica la estructura de arranque; no es una solución terminada.

## Entrega de cada hito

En Classroom entrega el repositorio propio del equipo, el tag semanal solicitado, el SHA exacto y el reporte de `make verify`. El repositorio debe conservar el historial y la evidencia de participación técnica de cada integrante.
