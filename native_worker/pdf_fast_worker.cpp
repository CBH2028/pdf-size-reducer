#define NOMINMAX

#include <mupdf/classes.h>
#include <mupdf/classes2.h>

#include <windows.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace fs = std::filesystem;

namespace {

constexpr int kProtocolVersion = 1;
constexpr int kMaximumThreads = 12;

struct RenderTask {
    int id = 0;
    int page = 0;
    float x0 = 0;
    float y0 = 0;
    float x1 = 0;
    float y1 = 0;
    int dpi = 180;
    int quality = 85;
    std::string filename;
};

std::string utf8(const std::wstring& value) {
    if (value.empty()) {
        return {};
    }
    const int size = WideCharToMultiByte(
        CP_UTF8, WC_ERR_INVALID_CHARS, value.data(),
        static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr);
    if (size <= 0) {
        throw std::runtime_error("Unable to convert a Windows path to UTF-8.");
    }
    std::string result(static_cast<size_t>(size), '\0');
    WideCharToMultiByte(
        CP_UTF8, WC_ERR_INVALID_CHARS, value.data(),
        static_cast<int>(value.size()), result.data(), size, nullptr, nullptr);
    return result;
}

std::string json_escape(const std::string& value) {
    std::ostringstream output;
    for (const unsigned char character : value) {
        switch (character) {
        case '\\': output << "\\\\"; break;
        case '"': output << "\\\""; break;
        case '\b': output << "\\b"; break;
        case '\f': output << "\\f"; break;
        case '\n': output << "\\n"; break;
        case '\r': output << "\\r"; break;
        case '\t': output << "\\t"; break;
        default:
            if (character < 0x20) {
                output << "\\u" << std::hex << std::setw(4)
                       << std::setfill('0') << static_cast<int>(character)
                       << std::dec;
            } else {
                output << character;
            }
        }
    }
    return output.str();
}

std::vector<std::string> split_tabs(const std::string& line) {
    std::vector<std::string> values;
    std::string value;
    std::istringstream input(line);
    while (std::getline(input, value, '\t')) {
        values.push_back(value);
    }
    return values;
}

std::vector<RenderTask> read_manifest(const fs::path& path) {
    std::ifstream input(path, std::ios::binary);
    if (!input) {
        throw std::runtime_error("Unable to open render manifest.");
    }
    std::vector<RenderTask> tasks;
    std::string line;
    int line_number = 0;
    while (std::getline(input, line)) {
        ++line_number;
        if (!line.empty() && line.back() == '\r') {
            line.pop_back();
        }
        if (line.empty() || line.front() == '#') {
            continue;
        }
        const auto fields = split_tabs(line);
        if (fields.size() != 10) {
            throw std::runtime_error(
                "Malformed manifest row " + std::to_string(line_number) + ".");
        }
        RenderTask task;
        task.id = std::stoi(fields[0]);
        task.page = std::stoi(fields[1]);
        task.x0 = std::stof(fields[2]);
        task.y0 = std::stof(fields[3]);
        task.x1 = std::stof(fields[4]);
        task.y1 = std::stof(fields[5]);
        task.dpi = std::clamp(std::stoi(fields[6]), 24, 1200);
        task.quality = std::clamp(std::stoi(fields[7]), 35, 100);
        task.filename = fields[8];
        // Field 9 is reserved for future protocol-compatible options.
        if (task.page < 0 || task.x1 <= task.x0 || task.y1 <= task.y0 ||
            task.filename.empty() || task.filename.find_first_of("/\\") != std::string::npos) {
            throw std::runtime_error(
                "Invalid render task on manifest row " +
                std::to_string(line_number) + ".");
        }
        tasks.push_back(std::move(task));
    }
    return tasks;
}

void render_task(
    const mupdf::FzDocument& document,
    const RenderTask& task,
    const fs::path& output_directory) {
    const float scale = static_cast<float>(task.dpi) / 72.0F;
    mupdf::FzPage page = document.fz_load_page(task.page);
    mupdf::FzMatrix matrix = mupdf::FzMatrix::fz_scale(scale, scale);
    mupdf::FzRect clip(task.x0, task.y0, task.x1, task.y1);
    mupdf::FzRect transformed(clip, matrix);
    mupdf::FzIrect bounds(transformed);
    if (bounds.fz_is_empty_irect()) {
        throw std::runtime_error("The requested Figure region is empty.");
    }

    mupdf::FzColorspace rgb(mupdf::FzColorspace::Fixed_RGB);
    mupdf::FzSeparations separations;
    mupdf::FzPixmap pixmap(rgb, bounds, separations, 0);
    pixmap.fz_clear_pixmap_with_value(255);

    // Apply the page transform exactly once. Passing it both to the draw
    // device and fz_run_page_contents would scale coordinates twice and crop
    // a completely different part of the page.
    mupdf::FzMatrix identity;
    mupdf::FzDevice draw(identity, pixmap, bounds);
    std::vector<fz_rect> cull_rectangles{*transformed.internal()};
    mupdf::FzDevice without_text(draw, cull_rectangles);
    mupdf::FzCookie cookie;
    page.fz_run_page_contents(without_text, matrix, cookie);
    without_text.fz_close_device();

    const fs::path output_path = output_directory / fs::u8path(task.filename);
    const std::string output_utf8 = utf8(output_path.wstring());
    pixmap.fz_save_pixmap_as_jpeg(output_utf8.c_str(), task.quality);
}

class RenderPool {
public:
    RenderPool(const fs::path& input_path, int requested_threads)
        : input_utf8_(utf8(input_path.wstring())),
          thread_count_(std::max(1, std::min(kMaximumThreads, requested_threads))) {
        workers_.reserve(static_cast<size_t>(thread_count_));
        for (int worker_number = 0; worker_number < thread_count_; ++worker_number) {
            workers_.emplace_back(&RenderPool::worker_loop, this, worker_number);
        }
        std::cout << "{\"type\":\"hello\",\"protocol\":"
                  << kProtocolVersion << ",\"threads\":" << thread_count_
                  << "}" << std::endl;
    }

    ~RenderPool() {
        {
            std::lock_guard<std::mutex> lock(state_mutex_);
            stopping_ = true;
            ++generation_;
        }
        start_condition_.notify_all();
        for (auto& worker : workers_) {
            if (worker.joinable()) {
                worker.join();
            }
        }
    }

    int run_batch(
        const fs::path& manifest_path,
        const fs::path& output_directory) {
        auto tasks = read_manifest(manifest_path);
        fs::create_directories(output_directory);
        const auto started = std::chrono::steady_clock::now();
        {
            std::lock_guard<std::mutex> lock(state_mutex_);
            tasks_ = std::move(tasks);
            output_directory_ = output_directory;
            next_task_.store(0);
            completed_.store(0);
            failed_.store(false);
            first_error_.clear();
            workers_pending_ = thread_count_;
            ++generation_;
        }
        start_condition_.notify_all();
        {
            std::unique_lock<std::mutex> lock(state_mutex_);
            finish_condition_.wait(lock, [&] { return workers_pending_ == 0; });
        }

        const auto elapsed =
            std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::steady_clock::now() - started);
        std::lock_guard<std::mutex> output_lock(output_mutex_);
        if (failed_.load()) {
            std::cout << "{\"type\":\"result\",\"ok\":false,\"completed\":"
                      << completed_.load() << ",\"elapsed_ms\":"
                      << elapsed.count() << ",\"message\":\""
                      << json_escape(first_error_) << "\"}" << std::endl;
            return 4;
        }
        std::cout << "{\"type\":\"result\",\"ok\":true,\"completed\":"
                  << completed_.load() << ",\"elapsed_ms\":"
                  << elapsed.count() << "}" << std::endl;
        return 0;
    }

private:
    void fail(const std::string& message) {
        failed_.store(true);
        std::lock_guard<std::mutex> lock(state_mutex_);
        if (first_error_.empty()) {
            first_error_ = message;
        }
    }

    void worker_loop(int worker_number) {
        std::unique_ptr<mupdf::FzDocument> document;
        std::string startup_error;
        try {
            document = std::make_unique<mupdf::FzDocument>(input_utf8_.c_str());
        } catch (const std::exception& error) {
            startup_error = error.what();
        }

        size_t observed_generation = 0;
        while (true) {
            {
                std::unique_lock<std::mutex> lock(state_mutex_);
                start_condition_.wait(lock, [&] {
                    return stopping_ || generation_ != observed_generation;
                });
                if (stopping_) {
                    return;
                }
                observed_generation = generation_;
            }

            if (!startup_error.empty()) {
                fail(startup_error);
            } else {
                while (!failed_.load(std::memory_order_relaxed)) {
                    const size_t index = next_task_.fetch_add(1);
                    if (index >= tasks_.size()) {
                        break;
                    }
                    const RenderTask task = tasks_[index];
                    try {
                        render_task(*document, task, output_directory_);
                        const auto output_path =
                            output_directory_ / fs::u8path(task.filename);
                        const auto bytes = fs::file_size(output_path);
                        const int done = completed_.fetch_add(1) + 1;
                        std::lock_guard<std::mutex> lock(output_mutex_);
                        std::cout << "{\"type\":\"progress\",\"id\":"
                                  << task.id << ",\"completed\":" << done
                                  << ",\"total\":" << tasks_.size()
                                  << ",\"bytes\":" << bytes
                                  << ",\"worker\":" << worker_number
                                  << "}" << std::endl;
                    } catch (const std::exception& error) {
                        fail(error.what());
                        std::lock_guard<std::mutex> lock(output_mutex_);
                        std::cout << "{\"type\":\"task_error\",\"id\":"
                                  << task.id << ",\"message\":\""
                                  << json_escape(error.what()) << "\"}"
                                  << std::endl;
                    }
                }
            }

            {
                std::lock_guard<std::mutex> lock(state_mutex_);
                --workers_pending_;
                if (workers_pending_ == 0) {
                    finish_condition_.notify_one();
                }
            }
        }
    }

    std::string input_utf8_;
    int thread_count_;
    std::vector<std::thread> workers_;
    std::mutex state_mutex_;
    std::mutex output_mutex_;
    std::condition_variable start_condition_;
    std::condition_variable finish_condition_;
    std::vector<RenderTask> tasks_;
    fs::path output_directory_;
    std::atomic<size_t> next_task_{0};
    std::atomic<int> completed_{0};
    std::atomic<bool> failed_{false};
    bool stopping_ = false;
    size_t generation_ = 0;
    int workers_pending_ = 0;
    std::string first_error_;
};

int serve(const fs::path& input_path, int requested_threads) {
    RenderPool pool(input_path, requested_threads);
    std::string line;
    while (std::getline(std::cin, line)) {
        if (!line.empty() && line.back() == '\r') {
            line.pop_back();
        }
        if (line == "QUIT") {
            return 0;
        }
        const auto fields = split_tabs(line);
        if (fields.size() != 3 || fields[0] != "BATCH") {
            std::cout << "{\"type\":\"result\",\"ok\":false,"
                         "\"completed\":0,\"message\":\"Invalid server command.\"}"
                      << std::endl;
            continue;
        }
        pool.run_batch(fs::u8path(fields[1]), fs::u8path(fields[2]));
    }
    return 0;
}

std::wstring required_value(
    const std::vector<std::wstring>& arguments, const std::wstring& name) {
    for (size_t index = 0; index + 1 < arguments.size(); ++index) {
        if (arguments[index] == name) {
            return arguments[index + 1];
        }
    }
    throw std::runtime_error("Missing a required command-line option.");
}

int optional_integer(
    const std::vector<std::wstring>& arguments,
    const std::wstring& name,
    int fallback) {
    for (size_t index = 0; index + 1 < arguments.size(); ++index) {
        if (arguments[index] == name) {
            return std::stoi(arguments[index + 1]);
        }
    }
    return fallback;
}

}  // namespace

int wmain(int argc, wchar_t** argv) {
    SetConsoleOutputCP(CP_UTF8);
    try {
        std::vector<std::wstring> arguments(argv + 1, argv + argc);
        if (arguments.empty() ||
            (arguments[0] != L"render-batch" && arguments[0] != L"serve" &&
             arguments[0] != L"--version")) {
            std::cerr << "Usage: pdf_fast_worker render-batch --input PDF "
                         "--manifest TSV --output-dir DIR [--threads N]\n"
                         "   or: pdf_fast_worker serve --input PDF [--threads N]\n";
            return 2;
        }
        if (arguments[0] == L"--version") {
            std::cout << "{\"name\":\"pdf_fast_worker\",\"protocol\":"
                      << kProtocolVersion << "}" << std::endl;
            return 0;
        }

        mupdf::fz_register_document_handlers();
        const fs::path input_path(required_value(arguments, L"--input"));
        const int threads = optional_integer(
            arguments,
            L"--threads",
            static_cast<int>(std::max(1U, std::thread::hardware_concurrency())));
        if (arguments[0] == L"serve") {
            return serve(input_path, threads);
        }
        const fs::path manifest_path(required_value(arguments, L"--manifest"));
        const fs::path output_directory(
            required_value(arguments, L"--output-dir"));
        RenderPool pool(input_path, threads);
        return pool.run_batch(manifest_path, output_directory);
    } catch (const std::exception& error) {
        std::cout << "{\"type\":\"result\",\"ok\":false,\"completed\":0,"
                     "\"message\":\""
                  << json_escape(error.what()) << "\"}" << std::endl;
        return 3;
    }
}
