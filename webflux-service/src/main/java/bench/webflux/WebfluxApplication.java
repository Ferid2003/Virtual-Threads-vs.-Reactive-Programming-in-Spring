package bench.webflux;

import io.netty.channel.ChannelOption;
import io.r2dbc.spi.ConnectionFactory;
import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.http.ResponseEntity;
import org.springframework.http.client.reactive.ReactorClientHttpConnector;
import org.springframework.r2dbc.core.DatabaseClient;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.reactive.function.client.WebClient;
import reactor.core.publisher.Mono;
import reactor.netty.http.client.HttpClient;
import reactor.netty.resources.ConnectionProvider;

// Keep records, SQL and stub client limits identical to mvc-service/MvcApplication.java.
// The HTTP client differs on purpose (stated variable): Reactor Netty here, blocking JDK HttpClient in MVC.
@SpringBootApplication
@RestController
@EnableConfigurationProperties(WebfluxApplication.StubClient.class)
public class WebfluxApplication {

    public static void main(String[] args) {
        SpringApplication.run(WebfluxApplication.class, args);
    }

    record Order(long id, long customerId, String status, long totalCents, Instant createdAt) {}
    record Customer(long id, String name, String tier) {}
    record EnrichedOrder(Order order, Customer customer) {}

    @ConfigurationProperties("stub")
    record StubClient(String url, int maxConnections, Duration connectTimeout, Duration responseTimeout,
                      Duration acquireTimeout, Duration idleTimeout) {}

    static final String SQL = "SELECT id, customer_id, status, total_cents, created_at FROM orders WHERE id = :id";

    private final DatabaseClient db;
    private final WebClient stub;

    WebfluxApplication(ConnectionFactory connectionFactory, WebClient.Builder builder, StubClient props) {
        this.db = DatabaseClient.create(connectionFactory);
        ConnectionProvider pool = ConnectionProvider.builder("stub")
                .maxConnections(props.maxConnections())
                .pendingAcquireMaxCount(-1) // unbounded FIFO queue, like the fair semaphore in mvc-service
                .pendingAcquireTimeout(props.acquireTimeout())
                .maxIdleTime(props.idleTimeout())
                .build();
        HttpClient http = HttpClient.create(pool)
                .option(ChannelOption.CONNECT_TIMEOUT_MILLIS, (int) props.connectTimeout().toMillis())
                .responseTimeout(props.responseTimeout());
        this.stub = builder.baseUrl(props.url()).clientConnector(new ReactorClientHttpConnector(http)).build();
    }

    @GetMapping("/orders/{id}")
    Mono<ResponseEntity<Order>> order(@PathVariable long id) {
        return find(id).map(ResponseEntity::ok).defaultIfEmpty(ResponseEntity.notFound().build());
    }

    @GetMapping("/orders/{id}/enriched")
    Mono<ResponseEntity<EnrichedOrder>> enriched(@PathVariable long id) {
        return find(id)
                .flatMap(o -> stub.get().uri("/customers/{id}", o.customerId()).retrieve().bodyToMono(Customer.class)
                        .map(c -> new EnrichedOrder(o, c)))
                .map(ResponseEntity::ok).defaultIfEmpty(ResponseEntity.notFound().build());
    }

    private Mono<Order> find(long id) {
        return db.sql(SQL).bind("id", id)
                .map(r -> new Order(r.get(0, Long.class), r.get(1, Long.class), r.get(2, String.class), r.get(3, Long.class),
                        r.get(4, OffsetDateTime.class).toInstant()))
                .one();
    }
}
