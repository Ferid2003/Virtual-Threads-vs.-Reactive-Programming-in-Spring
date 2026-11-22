package bench.mvc;

import java.io.IOException;
import java.io.InterruptedIOException;
import java.net.http.HttpClient;
import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.Optional;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import javax.sql.DataSource;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.http.ResponseEntity;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.client.RestClient;

// Keep records, SQL and stub client limits identical to webflux-service/WebfluxApplication.java.
// The HTTP client differs on purpose (stated variable): blocking JDK HttpClient here, Reactor Netty in WebFlux.
@SpringBootApplication
@RestController
@EnableConfigurationProperties(MvcApplication.StubClient.class)
public class MvcApplication {

    public static void main(String[] args) {
        SpringApplication.run(MvcApplication.class, args);
    }

    record Order(long id, long customerId, String status, long totalCents, Instant createdAt) {}
    record Customer(long id, String name, String tier) {}
    record EnrichedOrder(Order order, Customer customer) {}

    @ConfigurationProperties("stub")
    record StubClient(String url, int maxConnections, Duration connectTimeout, Duration responseTimeout,
                      Duration acquireTimeout, Duration idleTimeout) {}

    static final String SQL = "SELECT id, customer_id, status, total_cents, created_at FROM orders WHERE id = :id";

    private final JdbcClient db;
    private final RestClient stub;

    MvcApplication(DataSource dataSource, RestClient.Builder builder, StubClient props) {
        this.db = JdbcClient.create(dataSource);

        // JDK HttpClient has no connection cap of its own. These JVM-wide properties (read when the client
        // classes load, i.e. right below) set the keep-alive idle timeout and idle pool size; the fair semaphore
        // caps in-flight requests, which on HTTP/1.1 equals open connections. Mirrors Reactor Netty's
        // maxConnections / FIFO pending queue / pendingAcquireTimeout / maxIdleTime in webflux-service.
        System.setProperty("jdk.httpclient.keepalive.timeout", String.valueOf(props.idleTimeout().toSeconds()));
        System.setProperty("jdk.httpclient.connectionPoolSize", String.valueOf(props.maxConnections()));
        HttpClient http = HttpClient.newBuilder()
                .version(HttpClient.Version.HTTP_1_1) // default HTTP_2 would attempt an h2c upgrade on every new connection
                .connectTimeout(props.connectTimeout())
                .build();
        JdkClientHttpRequestFactory factory = new JdkClientHttpRequestFactory(http);
        factory.setReadTimeout(props.responseTimeout());

        Semaphore connections = new Semaphore(props.maxConnections(), true);
        this.stub = builder.baseUrl(props.url()).requestFactory(factory)
                .requestInterceptor((request, body, execution) -> {
                    acquire(connections, props.acquireTimeout());
                    // ponytail: permit is released once headers arrive, before the (tiny) body is read;
                    // wrap the response and release on close() if bodies ever get large.
                    try {
                        return execution.execute(request, body);
                    } finally {
                        connections.release();
                    }
                })
                .build();
    }

    @GetMapping("/orders/{id}")
    ResponseEntity<Order> order(@PathVariable long id) {
        return ResponseEntity.of(find(id));
    }

    @GetMapping("/orders/{id}/enriched")
    ResponseEntity<EnrichedOrder> enriched(@PathVariable long id) {
        return ResponseEntity.of(find(id).map(o -> new EnrichedOrder(o,
                stub.get().uri("/customers/{id}", o.customerId()).retrieve().body(Customer.class))));
    }

    private Optional<Order> find(long id) {
        return db.sql(SQL).param("id", id)
                .query((rs, n) -> new Order(rs.getLong(1), rs.getLong(2), rs.getString(3), rs.getLong(4),
                        rs.getObject(5, OffsetDateTime.class).toInstant()))
                .optional();
    }

    private static void acquire(Semaphore connections, Duration timeout) throws IOException {
        try {
            if (!connections.tryAcquire(timeout.toMillis(), TimeUnit.MILLISECONDS)) {
                throw new IOException("stub connection limit: no connection within " + timeout);
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new InterruptedIOException();
        }
    }
}
