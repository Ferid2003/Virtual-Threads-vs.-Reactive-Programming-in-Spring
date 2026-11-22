# One Dockerfile for all services:  --build-arg SERVICE=mvc-service --build-arg JDK=21|25|27
# The jar is always compiled on JDK 21 (release 21), so every runtime JDK executes identical bytecode.
ARG JDK=21

FROM maven:3.9-eclipse-temurin-21 AS build
ARG SERVICE
WORKDIR /src
COPY ${SERVICE}/pom.xml .
RUN mvn -q -B dependency:go-offline
COPY ${SERVICE}/src src
RUN mvn -q -B -o package -DskipTests

FROM eclipse-temurin:${JDK}-jre
COPY --from=build /src/target/app.jar /app/app.jar
# JVM flags come from JAVA_TOOL_OPTIONS (set in docker-compose.yml).
ENTRYPOINT ["java", "-jar", "/app/app.jar"]
